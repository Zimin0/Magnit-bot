"""Constrained Qwen proposals and verified replacement of configured baskets."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from pathlib import Path
from random import Random
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
import yaml

from magnit_cart import Basket, CartError, CartSettings
from magnit_cli import MagnitCli, MagnitCliError


class CartAssistantError(CartError):
    """Safe to display; never contains raw model, CLI, or session data."""


class BasketProposal(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    basket_id: str | None
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


@dataclass(frozen=True)
class CartExecution:
    basket_id: str
    basket_name: str
    positions: dict[str, int]
    total: int | None
    unavailable: list[str]


class CartAssistant:
    def __init__(self, ollama: httpx.AsyncClient, cli: MagnitCli,
                 skill_path: Path | None = None, *, rng: Random | None = None):
        self.ollama = ollama
        self.cli = cli
        self.skill_path = (Path(skill_path) if skill_path is not None else
                           Path(__file__).resolve().parent / '.agents/skills/magnit-cart-assistant/SKILL.md')
        self.rng = rng if rng is not None else Random()

    def _load_skill(self) -> str:
        try:
            text = self.skill_path.read_text(encoding='utf-8')
            lines = text.splitlines()
            if not lines or lines[0] != '---':
                raise ValueError
            end = lines.index('---', 1)
            metadata = yaml.safe_load('\n'.join(lines[1:end]))
            if (not isinstance(metadata, dict) or metadata.get('name') != 'magnit-cart-assistant'
                    or not isinstance(metadata.get('description'), str)
                    or not metadata['description'].startswith('Use when')
                    or not '\n'.join(lines[end + 1:]).strip()):
                raise ValueError
            return text
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            raise CartAssistantError('Не удалось загрузить skill помощника корзины.') from None

    async def propose(self, text: str, settings: CartSettings) -> BasketProposal:
        if not settings.baskets:
            raise CartAssistantError('Нет настроенных именных корзин. Добавьте корзину в настройках.')
        skill = self._load_skill()
        catalog = [{
            'id': basket.id, 'name': basket.name, 'category': basket.category,
            'groups': [{'name': group.name, 'options': [option.name for option in group.options]}
                       for group in basket.groups],
        } for basket in settings.baskets]
        allowed_ids = {item['id'] for item in catalog}
        try:
            response = await self.ollama.post('/api/chat', json={
                'model': 'qwen3:8b', 'stream': False, 'think': False,
                'options': {'num_ctx': 4096, 'num_predict': 512},
                'format': BasketProposal.model_json_schema(),
                'messages': [
                    {'role': 'system', 'content': skill},
                    {'role': 'user', 'content': json.dumps({'request': text, 'baskets': catalog},
                                                         ensure_ascii=False)},
                ],
            })
            response.raise_for_status()
            payload = response.json()
            message = payload['message']
            if message['role'] != 'assistant' or not isinstance(message['content'], str):
                raise ValueError
            proposal = BasketProposal.model_validate_json(message['content'])
            if proposal.basket_id is not None and proposal.basket_id not in allowed_ids:
                raise ValueError
            return proposal
        except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError):
            raise CartAssistantError('Qwen не смогла предложить корзину. Проверьте Ollama и модель qwen3:8b.') from None

    def _positions(self, basket: Basket) -> dict[str, int]:
        try:
            # Settings models are mutable; revalidate before preparing a write.
            basket = Basket.model_validate(basket.model_dump())
        except ValidationError:
            raise CartAssistantError('Проверьте состав именной корзины в настройках.') from None
        positions: dict[str, int] = {}
        for group in basket.groups:
            chosen = (self.rng.sample(group.options, group.pick) if group.strategy == 'random'
                      else group.options[:group.pick])
            for option in chosen:
                positions[option.sku] = positions.get(option.sku, 0) + 1
        return positions

    @staticmethod
    def _valid_sku(value: object) -> bool:
        return isinstance(value, str) and value.isascii() and value.isdigit()

    @classmethod
    def _cart_positions(cls, payload: object) -> dict[str, int]:
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise CartAssistantError('CLI вернул корзину неизвестного формата.')
        positions: dict[str, int] = {}
        for item in payload['items']:
            if (not isinstance(item, dict) or not cls._valid_sku(item.get('goodId'))
                    or type(item.get('qnty')) is not int or item['qnty'] <= 0):
                raise CartAssistantError('CLI вернул корзину неизвестного формата.')
            sku = item['goodId']
            positions[sku] = positions.get(sku, 0) + item['qnty']
        return positions

    @classmethod
    def _checkout(cls, payload: object) -> tuple[int | None, list[str]]:
        error = 'CLI вернул проверку наличия и суммы неизвестного формата.'
        if not isinstance(payload, dict):
            raise CartAssistantError(error)
        summary = payload.get('summary', {})
        if not isinstance(summary, dict):
            raise CartAssistantError(error)
        total = summary.get('totalFinalPrice')
        if total is not None and (type(total) is not int or total < 0):
            raise CartAssistantError(error)
        items = payload.get('unavailableItems', [])
        if not isinstance(items, list):
            raise CartAssistantError(error)
        unavailable = []
        for item in items:
            if not isinstance(item, dict) or not cls._valid_sku(item.get('goodId')):
                raise CartAssistantError(error)
            if item['goodId'] not in unavailable:
                unavailable.append(item['goodId'])
        return total, unavailable

    async def execute(self, basket: Basket,
                      progress: Callable[[str], Awaitable[None]] | None = None) -> CartExecution:
        """Replace the entire cart; caller must first obtain fresh confirmation."""
        expected = self._positions(basket)
        try:
            if progress is not None:
                await progress('reading')
            self._cart_positions(await self.cli.cart())
            if progress is not None:
                await progress('replacing')
            await self.cli.cart_set(expected)
            if progress is not None:
                await progress('verifying')
            if self._cart_positions(await self.cli.cart()) != expected:
                raise CartAssistantError('Состав корзины после замены не совпал с ожидаемым. Проверьте корзину в приложении.')
            if progress is not None:
                await progress('checkout')
            total, unavailable = self._checkout(await self.cli.checkout_preview())
        except MagnitCliError:
            raise CartAssistantError('Не удалось подтвердить выполнение CLI. Проверьте текущую корзину в приложении.') from None
        return CartExecution(basket.id, basket.name, expected, total, unavailable)

