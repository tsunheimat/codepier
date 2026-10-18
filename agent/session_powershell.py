"""Persistent PowerShell control protocol, distinct from interactive terminal IO.

The fixed bootstrap reads JSON lines, dot-sources commands in its original scope,
redirects normal output away from control frames, and normalizes PowerShell text
output to bounded UTF-8 payloads. It never infers completion from a prompt.
"""
from __future__ import annotations

import base64
import json

# Keep this a fixed program: caller commands arrive as base64 JSON on stdin, not
# interpolated PowerShell syntax or an argv string interpreted by cmd.exe.
BOOTSTRAP = r'''
$__cp_utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $__cp_utf8
[Console]::OutputEncoding = $__cp_utf8
$OutputEncoding = $__cp_utf8
$__cp_input = [Console]::In
$__cp_control = [Console]::OpenStandardOutput()
$__cp_root = Join-Path ([IO.Path]::GetTempPath()) ('codepier-session-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($__cp_root)
function Read-CodePierOutput([string]$path) {
    $reader = New-Object IO.StreamReader($path, [Text.Encoding]::UTF8, $true)
    $encoder = (New-Object Text.UTF8Encoding($false)).GetEncoder()
    $chars = New-Object char[] 4096
    $bytes = New-Object byte[] 16384
    $buffer = New-Object IO.MemoryStream
    $total = [long]0
    try {
        do {
            $count = $reader.Read($chars, 0, $chars.Length)
            $size = $encoder.GetBytes($chars, 0, $count, $bytes, 0, ($count -eq 0))
            $total += $size
            $keep = [Math]::Min($size, 1048576 - $buffer.Length)
            if ($keep -gt 0) { $buffer.Write($bytes, 0, [int]$keep) }
        } while ($count -gt 0)
        return @{ total = $total; data = $buffer.ToArray() }
    } finally { $reader.Dispose(); $buffer.Dispose() }
}
try {
    $__cp_ready = $__cp_utf8.GetBytes("CPREADY`n")
    $__cp_control.Write($__cp_ready, 0, $__cp_ready.Length)
    $__cp_control.Flush()
    while ($null -ne ($__cp_line = $__cp_input.ReadLine())) {
        $__cp_request = $__cp_line | ConvertFrom-Json -ErrorAction Stop
        $__cp_id = [string]$__cp_request.id
        if ($__cp_id -notmatch '^[a-f0-9]{32}$') { throw 'Invalid control identifier' }
        $__cp_command = $__cp_utf8.GetString([Convert]::FromBase64String($__cp_request.command))
        $__cp_outfile = Join-Path $__cp_root ($__cp_id + '.out')
        $__cp_errfile = Join-Path $__cp_root ($__cp_id + '.err')
        [IO.File]::WriteAllText($__cp_outfile, '', $__cp_utf8)
        [IO.File]::WriteAllText($__cp_errfile, '', $__cp_utf8)
        $global:LASTEXITCODE = 0
        $__cp_status = 0
        try {
            # Dot sourcing, not &, is essential: variables and functions survive
            # into the next command in this same process and PowerShell scope.
            . ([ScriptBlock]::Create($__cp_command)) 1>$__cp_outfile 2>$__cp_errfile 3>&1 4>&1 5>&1 6>&1
            $__cp_ok = $?
            if ($global:LASTEXITCODE -ne 0) { $__cp_status = [int]$global:LASTEXITCODE }
            elseif (-not $__cp_ok) { $__cp_status = 1 }
        } catch {
            $__cp_status = 1
            $_ | Out-File -FilePath $__cp_errfile -Encoding utf8 -Append
        }
        $__cp_out = Read-CodePierOutput $__cp_outfile
        $__cp_err = Read-CodePierOutput $__cp_errfile
        $__cp_frame = $__cp_utf8.GetBytes("CP1 $__cp_id $__cp_status $($__cp_out.total) $($__cp_err.total)`n")
        $__cp_control.Write($__cp_frame, 0, $__cp_frame.Length)
        $__cp_control.Write($__cp_out.data, 0, $__cp_out.data.Length)
        $__cp_control.Write($__cp_err.data, 0, $__cp_err.data.Length)
        $__cp_control.Flush()
        Remove-Item -LiteralPath $__cp_outfile, $__cp_errfile -Force -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item -LiteralPath $__cp_root -Recurse -Force -ErrorAction SilentlyContinue
}
'''


def argv(executable: str) -> list[str]:
    encoded = base64.b64encode(BOOTSTRAP.encode('utf-16le')).decode('ascii')
    return [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]


def command(identifier: str, text: str) -> bytes:
    return (json.dumps({'id': identifier, 'command': base64.b64encode(text.encode('utf-8')).decode('ascii')},
                       separators=(',', ':')) + '\n').encode('ascii')
