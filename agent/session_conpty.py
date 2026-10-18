"""Owned Windows ConPTY channels with synchronous, confirmed UTF-8 writes.

Only the Windows console API is used here. In particular an overlapped library's
queued write count is not treated as an acknowledgement for the current input.
The process starts suspended; SessionManager attaches its retained Job before
resuming it. Output and input are serviced on separate IO threads by the manager.

API/lifetime reference: Microsoft, Creating a Pseudoconsole Session and
ClosePseudoConsole (close or drain output before closing the console).
"""
from __future__ import annotations

import ctypes as C
import os
import subprocess
import threading

from shared.util import DevError

DWORD, WORD, HANDLE = C.c_uint32, C.c_uint16, C.c_void_p


class COORD(C.Structure):
    _fields_ = [('X', C.c_int16), ('Y', C.c_int16)]


class STARTUPINFO(C.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', C.c_wchar_p), ('lpDesktop', C.c_wchar_p),
        ('lpTitle', C.c_wchar_p), ('dwX', DWORD), ('dwY', DWORD), ('dwXSize', DWORD),
        ('dwYSize', DWORD), ('dwXCountChars', DWORD), ('dwYCountChars', DWORD),
        ('dwFillAttribute', DWORD), ('dwFlags', DWORD), ('wShowWindow', WORD),
        ('cbReserved2', WORD), ('lpReserved2', C.c_void_p), ('hStdInput', HANDLE),
        ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [('StartupInfo', STARTUPINFO), ('lpAttributeList', C.c_void_p)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE), ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


def kernel():
    api = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreatePipe': ([C.POINTER(HANDLE), C.POINTER(HANDLE), C.c_void_p, DWORD], C.c_int),
        'CloseHandle': ([HANDLE], C.c_int),
        'CreatePseudoConsole': ([COORD, HANDLE, HANDLE, DWORD, C.POINTER(HANDLE)], C.c_int32),
        'ResizePseudoConsole': ([HANDLE, COORD], C.c_int32),
        'ClosePseudoConsole': ([HANDLE], None),
        'InitializeProcThreadAttributeList': ([C.c_void_p, DWORD, DWORD, C.POINTER(C.c_size_t)], C.c_int),
        'UpdateProcThreadAttribute': ([C.c_void_p, DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], C.c_int),
        'DeleteProcThreadAttributeList': ([C.c_void_p], None),
        'CreateProcessW': ([C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p, C.c_int, DWORD,
                            C.c_void_p, C.c_wchar_p, C.POINTER(STARTUPINFOEX), C.POINTER(PROCESS_INFORMATION)], C.c_int),
        'ResumeThread': ([HANDLE], DWORD),
        'TerminateProcess': ([HANDLE, DWORD], C.c_int),
        'GetExitCodeProcess': ([HANDLE, C.POINTER(DWORD)], C.c_int),
        'PeekNamedPipe': ([HANDLE, C.c_void_p, DWORD, C.c_void_p, C.POINTER(DWORD), C.c_void_p], C.c_int),
        'ReadFile': ([HANDLE, C.c_void_p, DWORD, C.POINTER(DWORD), C.c_void_p], C.c_int),
        'WriteFile': ([HANDLE, C.c_void_p, DWORD, C.POINTER(DWORD), C.c_void_p], C.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def capabilities():
    if os.name != 'nt':
        return {'available': False, 'reason': 'ConPTY requires Windows'}
    try:
        kernel()
    except (AttributeError, OSError) as exc:
        return {'available': False, 'reason': str(exc)}
    return {'available': True, 'backend': 'conpty', 'transport': 'win32-synchronous'}


def checked(success):
    if not success:
        raise C.WinError(C.get_last_error())


def checked_hresult(value):
    if value < 0:
        raise OSError(f'ConPTY operation failed (HRESULT 0x{value & 0xffffffff:08x})')


class Console:
    def __init__(self, arguments, cwd, env, rows, cols):
        self.api = kernel()
        self.input = self.output = self.console = self.process = self.thread = None
        self.closed = False
        self.read_lock, self.write_lock = threading.Lock(), threading.Lock()
        input_read, output_write = HANDLE(), HANDLE()
        input_write, output_read, console = HANDLE(), HANDLE(), HANDLE()
        attributes = None
        attributes_ready = False
        info = PROCESS_INFORMATION()
        try:
            checked(self.api.CreatePipe(C.byref(input_read), C.byref(input_write), None, 65536))
            self.input = input_write.value
            checked(self.api.CreatePipe(C.byref(output_read), C.byref(output_write), None, 65536))
            self.output = output_read.value
            checked_hresult(self.api.CreatePseudoConsole(COORD(cols, rows), input_read, output_write, 0, C.byref(console)))
            self.console = console.value
            size = C.c_size_t()
            self.api.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
            if not size.value:
                raise C.WinError(C.get_last_error())
            attributes = C.create_string_buffer(size.value)
            checked(self.api.InitializeProcThreadAttributeList(attributes, 1, 0, C.byref(size)))
            attributes_ready = True
            checked(self.api.UpdateProcThreadAttribute(attributes, 0, 0x00020016,
                self.console, C.sizeof(HANDLE), None, None))
            startup = STARTUPINFOEX()
            startup.StartupInfo.cb = C.sizeof(startup)
            # A service/test host can have redirected std handles even when
            # ordinary handle inheritance is disabled. Explicit NULL handles
            # let console attachment supply CONIN$/CONOUT$ instead of copying
            # the parent's capture pipes/files (which may already be at EOF).
            # See Microsoft GetStdHandle: Attach/detach behavior.
            startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = None
            startup.StartupInfo.hStdOutput = None
            startup.StartupInfo.hStdError = None
            startup.lpAttributeList = C.cast(attributes, C.c_void_p)
            command = C.create_unicode_buffer(subprocess.list2cmdline(arguments))
            # A double-NUL terminated Unicode environment, not inherited handles.
            environment = C.create_unicode_buffer(''.join(f'{name}={value}\0'
                for name, value in sorted(env.items(), key=lambda item: item[0].upper())) + '\0')
            checked(self.api.CreateProcessW(arguments[0], command, None, None, False,
                0x00080000 | 0x00000400 | 0x00000004, environment, cwd,
                C.byref(startup), C.byref(info)))
            self.process, self.thread, self.pid = info.hProcess, info.hThread, int(info.dwProcessId)
        except BaseException:
            if info.hProcess:
                self.api.TerminateProcess(info.hProcess, 1)
                self.process, self.thread = info.hProcess, info.hThread
            self.close()
            raise
        finally:
            if attributes_ready:
                self.api.DeleteProcThreadAttributeList(attributes)
            # Console owns copies after child creation. Retaining our copies
            # prevents proper EOF and can deadlock terminal shutdown.
            for handle in (input_read.value, output_write.value):
                if handle:
                    self.api.CloseHandle(handle)

    def resume(self):
        if self.api.ResumeThread(self.thread) == 0xffffffff:
            raise C.WinError(C.get_last_error())
        self.api.CloseHandle(self.thread)
        self.thread = None

    def read(self, blocking=False):
        if blocking:
            raise ValueError('The Session output loop uses nonblocking reads')
        with self.read_lock:
            if self.closed or self.output is None:
                return b''
            available = DWORD()
            if not self.api.PeekNamedPipe(self.output, None, 0, None, C.byref(available), None):
                if C.get_last_error() in {109, 232, 233}:
                    return b''
                raise C.WinError(C.get_last_error())
            count = min(available.value, 32768)
            if not count:
                return b''
            data, read = C.create_string_buffer(count), DWORD()
            checked(self.api.ReadFile(self.output, data, count, C.byref(read), None))
            return data.raw[:read.value]

    def write(self, text):
        data = text.encode('utf-8')
        with self.write_lock:
            if self.closed or self.input is None:
                raise BrokenPipeError('The original terminal input pipe is closed')
            # Synchronous WriteFile returns the actual bytes of THIS write. The
            # caller sends at most 65536 bytes and never retries partial input.
            written = DWORD()
            buffer = C.create_string_buffer(data)
            checked(self.api.WriteFile(self.input, buffer, len(data), C.byref(written), None))
            return int(written.value)

    def set_size(self, cols, rows):
        if self.closed:
            raise BrokenPipeError('The original terminal is closed')
        checked_hresult(self.api.ResizePseudoConsole(self.console, COORD(cols, rows)))

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Closing output BEFORE ClosePseudoConsole is the documented nonblocking
        # teardown path on Windows versions preceding Windows 11 24H2. It also
        # breaks the peer of any outstanding input write; do not hold write_lock.
        with self.read_lock:
            if self.output is not None:
                self.api.CloseHandle(self.output)
                self.output = None
        if self.console is not None:
            self.api.ClosePseudoConsole(self.console)
            self.console = None
        with self.write_lock:
            if self.input is not None:
                self.api.CloseHandle(self.input)
                self.input = None
        for name in ('thread', 'process'):
            handle = getattr(self, name)
            if handle is not None:
                self.api.CloseHandle(handle)
                setattr(self, name, None)


def create_pty(arguments, cwd, env, rows, cols):
    if not capabilities()['available']:
        raise DevError('PTY_PLATFORM_UNSUPPORTED', 'This Runner has no Windows pseudoconsole API', 409)
    return Console(arguments, cwd, env, rows, cols)
