"""Allowlisted, JSON-only subprocess boundary for the private Magnit CLI."""

import asyncio
from collections.abc import Mapping
import json
import math
import os
from pathlib import Path
import sys
from types import MappingProxyType


class MagnitCliError(Exception):
    """A safe error that may be shown without exposing CLI/session data."""


CLI_CONTEXT_KEYS = frozenset({
    'MAGNIT_DEVICE_ID',
    'MAGNIT_DEVICE_TAG',
    'MAGNIT_PERMISSIONS_JWT',
    'MAGNIT_STORE_CODE',
    'MAGNIT_ADDRESS_ID',
    'MAGNIT_SERVICE',
    'MAGNIT_DELIVERY_TYPE',
})


class MagnitCli:
    def __init__(self, cli_dir: str | Path | None = None, *, context: Mapping[str, str],
                 timeout: float = 60.0):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise MagnitCliError('CLI timeout must be a finite positive number.')
        self.timeout = timeout
        configured_dir = cli_dir if cli_dir is not None else os.environ.get('MAGNIT_CLI_DIR')
        if not configured_dir:
            raise MagnitCliError('Magnit CLI directory is not configured.')
        try:
            self.cli_dir = Path(configured_dir).resolve()
        except (OSError, ValueError, TypeError):
            raise MagnitCliError('Magnit CLI directory is invalid.') from None
        if (not isinstance(context, Mapping) or set(context) != CLI_CONTEXT_KEYS
                or any(not isinstance(value, str) or not value.strip() for value in context.values())):
            raise MagnitCliError('Magnit CLI context is invalid.')
        self._context = MappingProxyType(dict(context))

    async def run(self, command: str, *args: str) -> object:
        if not isinstance(command, str) or command not in ('cart', 'cart-set', 'checkout-preview'):
            raise MagnitCliError('Magnit CLI command is not allowed.')
        if any(not isinstance(arg, str) for arg in args):
            raise MagnitCliError('Magnit CLI arguments must be strings.')
        entrypoint = self.cli_dir / 'magnit.py'
        if not entrypoint.is_file():
            raise MagnitCliError('Magnit CLI entrypoint is unavailable.')
        environment = os.environ.copy()
        environment.update(self._context)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(entrypoint), command, *args,
                cwd=str(self.cli_dir), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except (OSError, ValueError):
            raise MagnitCliError('Magnit CLI could not be started.') from None
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except TimeoutError:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise MagnitCliError('Magnit CLI timed out.') from None
        if process.returncode != 0:
            raise MagnitCliError('Magnit CLI command failed.')
        if not stdout.strip():
            raise MagnitCliError('Magnit CLI returned an empty response.')
        try:
            return json.loads(stdout, parse_constant=self._reject_constant)
        except (ValueError, UnicodeError):
            raise MagnitCliError('Magnit CLI returned invalid JSON.') from None

    async def cart(self) -> object:
        return await self.run('cart')

    async def cart_set(self, positions: Mapping[str, int]) -> object:
        if not isinstance(positions, Mapping) or not positions:
            raise MagnitCliError('Cart positions must be a non-empty mapping.')
        if any(
            not isinstance(sku, str) or not sku.isascii() or not sku.isdigit()
            or type(quantity) is not int or quantity <= 0
            for sku, quantity in positions.items()
        ):
            raise MagnitCliError('Cart positions require digit-only SKUs and positive integer quantities.')
        return await self.run('cart-set', *(
            f'{sku}:{positions[sku]}' for sku in sorted(positions)
        ), '--yes')

    async def checkout_preview(self) -> object:
        return await self.run('checkout-preview')

    @staticmethod
    def _reject_constant(value: str) -> object:
        raise ValueError('Non-finite JSON number.')

