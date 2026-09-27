"""Real POSIX PTY and portable ConPTY adapter contract checks."""
import os
import sys
import time
from unittest.mock import Mock

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX runtime')
def test_posix_real_read_write_resize_and_reap():
    from hermes_cli.pty_bridge import PtyBridge
    bridge = PtyBridge.spawn([sys.executable, '-u', '-c', "import sys; print('READY'); print('GOT:'+sys.stdin.readline().strip())"])
    pid = bridge.pid
    try:
        bridge.resize(120, 40)
        bridge.write('中文\n'.encode())
        output = b''
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = bridge.read(0.1)
            if chunk is None:
                break
            output += chunk
        assert 'GOT:中文'.encode() in output
    finally:
        bridge.close()
        bridge.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_windows_adapter_contract_including_split_utf8(monkeypatch):
    from hermes_cli import win_pty_bridge as win
    proc = Mock(pid=123)
    proc.read.side_effect = ['中文', EOFError()]
    spawn = Mock(return_value=proc)
    monkeypatch.setattr(win, '_PTY_AVAILABLE', True)
    monkeypatch.setattr(win, 'PtyProcess', Mock(spawn=spawn))
    bridge = win.WinPtyBridge.spawn(['node', 'entry.js'], env={'PROFILE': 'test'}, cols=99999, rows=-1)
    assert spawn.call_args.kwargs['dimensions'] == (1, 2000)
    assert spawn.call_args.kwargs['env']['PROFILE'] == 'test'
    encoded = '中'.encode()
    bridge.write(encoded[:1])
    bridge.write(encoded[1:])
    assert ''.join(call.args[0] for call in proc.write.call_args_list) == '中'
    assert bridge.read() == '中文'.encode()
    assert bridge.read() is None
    bridge.resize(120, 40)
    proc.setwinsize.assert_called_with(40, 120)
    bridge.close(); bridge.close()
    proc.terminate.assert_called_once_with(force=True)


@pytest.mark.windows_only
def test_windows_real_runtime():
    from hermes_cli.win_pty_bridge import WinPtyBridge
    bridge = WinPtyBridge.spawn([sys.executable, '-u', '-c', "print('C1-CONPTY')"])
    try:
        output = b''
        for _ in range(100):
            chunk = bridge.read(0.1)
            if chunk is None:
                break
            output += chunk
            if b'C1-CONPTY' in output:
                break
        assert b'C1-CONPTY' in output
    finally:
        bridge.close()
