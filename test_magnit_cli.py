import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from magnit_cli import MagnitCli, MagnitCliError


CLI_CONTEXT = {
    'MAGNIT_DEVICE_ID': 'captured-device-id',
    'MAGNIT_DEVICE_TAG': 'captured-device-tag',
    'MAGNIT_PERMISSIONS_JWT': 'captured-permissions-jwt',
    'MAGNIT_STORE_CODE': 'captured-store-code',
    'MAGNIT_ADDRESS_ID': 'captured-address-id',
    'MAGNIT_SERVICE': 'express',
    'MAGNIT_DELIVERY_TYPE': 'delivery',
}


class MagnitCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix='magnit cli ', dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.cli_dir = Path(self.directory.name).resolve()
        self.entrypoint = self.cli_dir / 'magnit.py'
        self.entrypoint.write_text('# Test fixture; never executed.\n', encoding='utf-8')
        self.process = SimpleNamespace(
            returncode=0,
            communicate=AsyncMock(return_value=(b'{"items": []}', b'')),
            kill=Mock(),
        )
        self.spawn = self.enterContext(patch(
            'asyncio.create_subprocess_exec', new_callable=AsyncMock,
            return_value=self.process,
        ))
        self.cli = MagnitCli(self.cli_dir, context=CLI_CONTEXT)

    async def test_uses_current_interpreter_fixed_cwd_and_separate_arguments(self):
        result = await self.cli.run('cart', 'literal; & $(data)')

        self.assertEqual(result, {'items': []})
        self.assertEqual(self.spawn.await_args.args, (
            sys.executable, str(self.entrypoint), 'cart', 'literal; & $(data)',
        ))
        self.assertEqual(self.spawn.await_args.kwargs['cwd'], str(self.cli_dir))
        self.assertEqual(self.spawn.await_args.kwargs['stdout'], asyncio.subprocess.PIPE)
        self.assertEqual(self.spawn.await_args.kwargs['stderr'], asyncio.subprocess.PIPE)

    async def test_captured_context_overrides_parent_environment_for_every_subprocess(self):
        with patch.dict(os.environ, {
            'MAGNIT_DEVICE_ID': 'stale-device-id',
            'UNRELATED_PARENT_VALUE': 'preserved',
        }, clear=True):
            cli = MagnitCli(self.cli_dir, context=CLI_CONTEXT)
            await cli.cart()
            first_environment = self.spawn.await_args.kwargs['env']
            first_environment['MAGNIT_DEVICE_ID'] = 'mutated-after-spawn'
            await cli.checkout_preview()
            second_environment = self.spawn.await_args.kwargs['env']

        self.assertEqual(second_environment['MAGNIT_DEVICE_ID'], 'captured-device-id')
        self.assertEqual(second_environment['UNRELATED_PARENT_VALUE'], 'preserved')
        self.assertEqual({key: second_environment[key] for key in CLI_CONTEXT}, CLI_CONTEXT)
        self.assertIsNot(first_environment, second_environment)

    async def test_captured_context_is_an_immutable_constructor_snapshot(self):
        supplied = dict(CLI_CONTEXT)
        cli = MagnitCli(self.cli_dir, context=supplied)
        supplied['MAGNIT_DEVICE_ID'] = 'mutated-input'

        with self.assertRaises(TypeError):
            cli._context['MAGNIT_DEVICE_ID'] = 'mutated-instance'
        await cli.cart()

        self.assertFalse(hasattr(cli, 'context'))
        self.assertEqual(self.spawn.await_args.kwargs['env']['MAGNIT_DEVICE_ID'],
                         'captured-device-id')

    async def test_context_is_required(self):
        with self.assertRaises(TypeError):
            MagnitCli(self.cli_dir)

    async def test_rejects_partial_unknown_or_empty_captured_context(self):
        invalid_contexts = (
            {key: value for key, value in CLI_CONTEXT.items() if key != 'MAGNIT_ADDRESS_ID'},
            {**CLI_CONTEXT, 'MAGNIT_UNKNOWN': 'value'},
            {**CLI_CONTEXT, 'MAGNIT_DEVICE_ID': ''},
            {**CLI_CONTEXT, 'MAGNIT_DEVICE_ID': '   '},
            {**CLI_CONTEXT, 'MAGNIT_DEVICE_ID': None},
            [],
        )
        for context in invalid_contexts:
            with self.subTest(context=context):
                with self.assertRaises(MagnitCliError):
                    MagnitCli(self.cli_dir, context=context)
        self.spawn.assert_not_awaited()

    async def test_reads_cli_directory_from_environment(self):
        with patch.dict(os.environ, {'MAGNIT_CLI_DIR': str(self.cli_dir)}):
            await MagnitCli(context=CLI_CONTEXT).cart()

        self.assertEqual(self.spawn.await_args.args, (
            sys.executable, str(self.entrypoint), 'cart',
        ))
        self.assertEqual(self.spawn.await_args.kwargs['cwd'], str(self.cli_dir))

    async def test_rejects_missing_directory_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MagnitCliError):
                await MagnitCli(context=CLI_CONTEXT).cart()
        self.spawn.assert_not_awaited()

    async def test_rejects_commands_outside_allowlist(self):
        for command in ('checkout', 'order-create', '', 'cart; checkout', None, []):
            with self.subTest(command=command):
                with self.assertRaises(MagnitCliError):
                    await self.cli.run(command)
        self.spawn.assert_not_awaited()

    async def test_rejects_missing_or_directory_entrypoint_before_spawning(self):
        self.entrypoint.unlink()
        for entrypoint_is_directory in (False, True):
            with self.subTest(entrypoint_is_directory=entrypoint_is_directory):
                if entrypoint_is_directory:
                    self.entrypoint.mkdir()
                with self.assertRaises(MagnitCliError):
                    await self.cli.cart()
        self.spawn.assert_not_awaited()

    async def test_resolves_relative_cli_directory_before_using_as_cwd(self):
        relative = os.path.relpath(self.cli_dir, Path.cwd())
        await MagnitCli(relative, context=CLI_CONTEXT).cart()
        self.assertEqual(self.spawn.await_args.args[1], str(self.entrypoint))
        self.assertEqual(self.spawn.await_args.kwargs['cwd'], str(self.cli_dir))

    async def test_timeout_terminates_and_reaps_child_without_leaking_details(self):
        self.process.communicate.side_effect = [
            asyncio.TimeoutError('private-session-value'), (b'', b'private-session-value'),
        ]
        with self.assertRaises(MagnitCliError) as raised:
            await self.cli.cart()

        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.communicate.await_count, 2)
        self.assert_safe_error(raised.exception)

    async def test_timeout_cleanup_is_bounded_when_pipes_remain_open(self):
        release_pipes = asyncio.Event()

        async def communicate():
            if self.process.communicate.await_count == 1:
                raise TimeoutError('private-session-value')
            await release_pipes.wait()
            return b'', b'private-session-value'

        self.process.communicate.side_effect = communicate
        task = asyncio.create_task(MagnitCli(self.cli_dir, timeout=0.001,
                                             context=CLI_CONTEXT).cart())
        try:
            done, _pending = await asyncio.wait({task}, timeout=0.1)
            self.assertIn(task, done, 'Timeout cleanup must not wait indefinitely for open pipes.')
            with self.assertRaises(MagnitCliError) as raised:
                await task
            self.assert_safe_error(raised.exception)
            self.process.kill.assert_called_once_with()
            self.assertEqual(self.process.communicate.await_count, 2)
        finally:
            release_pipes.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_call_cleanup_is_bounded_when_pipes_remain_open(self):
        communicate_started = asyncio.Event()
        release_pipes = asyncio.Event()

        async def communicate():
            communicate_started.set()
            await release_pipes.wait()
            return b'', b'private-session-value'

        self.process.communicate.side_effect = communicate
        task = asyncio.create_task(MagnitCli(self.cli_dir, timeout=0.01,
                                             context=CLI_CONTEXT).cart())
        try:
            await communicate_started.wait()
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=0.1)
            self.assertIn(task, done, 'Cancellation cleanup must not wait indefinitely for open pipes.')
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.process.kill.assert_called_once_with()
            self.assertEqual(self.process.communicate.await_count, 2)
        finally:
            release_pipes.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_configured_timeout_limits_communication(self):
        with patch('asyncio.wait_for', new_callable=AsyncMock) as wait:
            async def collect(awaitable, timeout):
                self.assertEqual(timeout, 0.125)
                return await awaitable
            wait.side_effect = collect
            await MagnitCli(self.cli_dir, timeout=0.125, context=CLI_CONTEXT).cart()
        wait.assert_awaited_once()

    async def test_rejects_nonfinite_or_nonpositive_timeout_before_spawning(self):
        for timeout in (0, -1, float('inf'), float('nan'), True, '30'):
            with self.subTest(timeout=timeout):
                with self.assertRaises(MagnitCliError):
                    await MagnitCli(self.cli_dir, timeout=timeout, context=CLI_CONTEXT).cart()
        self.spawn.assert_not_awaited()

    async def test_nonzero_exit_does_not_expose_outputs_or_environment(self):
        self.process.returncode = 1
        self.process.communicate.return_value = (
            b'{"token": "private-session-value"}', b'private-session-value' * 1000,
        )
        with patch.dict(os.environ, {'MAGNIT_TOKEN': 'private-session-value'}):
            with self.assertRaises(MagnitCliError) as raised:
                await self.cli.cart()
        self.assert_safe_error(raised.exception)

    async def test_rejects_empty_malformed_nonfinite_and_invalid_unicode_json(self):
        for stdout in (b'', b' \n', b'private-session-value', b'{', b'NaN',
                       b'Infinity', b'-Infinity', b'{"value": NaN}', b'"\xff"'):
            with self.subTest(stdout=stdout):
                self.process.communicate.return_value = (stdout, b'private-session-value')
                with self.assertRaises(MagnitCliError) as raised:
                    await self.cli.cart()
                self.assert_safe_error(raised.exception)

    async def test_accepts_valid_json_scalars_and_arrays(self):
        for stdout, expected in ((b'null', None), (b'false', False),
                                 (b'42', 42), (b'[]', [])):
            with self.subTest(stdout=stdout):
                self.process.communicate.return_value = (stdout, b'')
                self.assertEqual(await self.cli.cart(), expected)

    async def test_spawn_failure_is_a_safe_domain_error(self):
        self.spawn.side_effect = OSError('private-session-value' * 1000)
        with self.assertRaises(MagnitCliError) as raised:
            await self.cli.cart()
        self.assert_safe_error(raised.exception)

    async def test_typed_read_helpers_supply_only_their_command(self):
        for helper, command in ((self.cli.cart, 'cart'),
                                (self.cli.checkout_preview, 'checkout-preview')):
            with self.subTest(command=command):
                self.spawn.reset_mock()
                self.assertEqual(await helper(), {'items': []})
                self.assertEqual(self.spawn.await_args.args, (
                    sys.executable, str(self.entrypoint), command,
                ))

    async def test_cart_set_sorts_positions_preserves_skus_and_appends_yes(self):
        self.assertEqual(await self.cli.cart_set({'20': 3, '003': 2, '10': 1}),
                         {'items': []})
        self.assertEqual(self.spawn.await_args.args, (
            sys.executable, str(self.entrypoint),
            'cart-set', '003:2', '10:1', '20:3', '--yes',
        ))

    async def test_cart_set_rejects_invalid_or_empty_mappings_without_spawning(self):
        invalid = (
            {}, None, [], [('123', 1)], {'123': 0}, {'123': -1},
            {'123': True}, {'123': 1.0}, {'123': '1'}, {'123': None},
            {'': 1}, {' 123': 1}, {'123:2': 1}, {'-123': 1},
            {'123; checkout': 1}, {123: 1}, {'\u00b2': 1}, {'\u0661\u0662\u0663': 1},
            {'123': 1, '456': 0},
        )
        for positions in invalid:
            with self.subTest(positions=positions):
                with self.assertRaises(MagnitCliError) as raised:
                    await self.cli.cart_set(positions)
                self.assert_safe_error(raised.exception)
        self.spawn.assert_not_awaited()

    def assert_safe_error(self, error):
        self.assertTrue(str(error))
        self.assertLessEqual(len(str(error)), 200)
        self.assertNotIn('private-session-value', str(error))
        self.assertNotIn(str(self.cli_dir), str(error))
        self.assertIsNone(error.__cause__)


if __name__ == '__main__':
    unittest.main()

