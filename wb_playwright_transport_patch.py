#!/usr/bin/env python3
"""
Playwright Transport 补丁
修复 Windows 上 asyncio.create_subprocess_exec 因安全策略无法创建 overlapped pipes 的问题。

使用方法：在导入 playwright 之前，先执行：
    from wb_playwright_transport_patch import apply_patch
    apply_patch()
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import threading
from typing import Callable, Dict, Optional, Union

from playwright._impl._helper import ParsedMessagePayload


def _get_stderr_fileno() -> Optional[int]:
    try:
        if sys.stderr is None or not hasattr(sys.stderr, "closed"):
            return None
        if sys.stderr.closed:
            return None
        return sys.stderr.fileno()
    except (NotImplementedError, AttributeError, io.UnsupportedOperation):
        if not hasattr(sys, "__stderr__") or not sys.__stderr__:
            return None
        return sys.__stderr__.fileno()


class PipeTransportPatched:
    """
    使用 subprocess.Popen 的 PipeTransport 替代品。
    避免 asyncio.create_subprocess_exec 在 Windows 上因安全策略失败的问题。
    """
    
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.on_message: Callable[[ParsedMessagePayload], None] = lambda _: None
        self.on_error_future: asyncio.Future = loop.create_future()
        self._stopped = False
        self._proc = None
        self._output = None
    
    def request_stop(self) -> None:
        self._stopped = True
        if self._output:
            try:
                self._output.close()
            except:
                pass
    
    def dispose(self) -> None:
        pass
    
    async def wait_until_stopped(self) -> None:
        await self._stopped_future
    
    async def connect(self) -> None:
        self._stopped_future: asyncio.Future = self._loop.create_future()
        
        try:
            from playwright._impl._driver import get_driver_env, compute_driver_executable
            
            env = get_driver_env()
            if getattr(sys, "frozen", False) or globals().get("__compiled__"):
                env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
            
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            
            executable_path, entrypoint_path = compute_driver_executable()
            
            # 使用 subprocess.Popen 而不是 asyncio.create_subprocess_exec
            self._proc = subprocess.Popen(
                [executable_path, entrypoint_path, "run-driver"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=_get_stderr_fileno(),
                env=env,
                startupinfo=startupinfo,
                bufsize=0,
            )
            
            self._output = self._proc.stdin
            
        except Exception as exc:
            self.on_error_future.set_exception(exc)
            raise exc
    
    async def run(self) -> None:
        assert self._proc.stdout
        assert self._proc.stdin
        try:
            while not self._stopped:
                try:
                    buffer = await self._loop.run_in_executor(
                        None, self._read_exact, 4
                    )
                    if self._stopped:
                        break
                    
                    length = int.from_bytes(buffer, byteorder="little", signed=False)
                    buffer = bytes(0)
                    
                    while length:
                        to_read = min(length, 32768)
                        data = await self._loop.run_in_executor(
                            None, self._read_exact, to_read
                        )
                        if self._stopped:
                            break
                        length -= to_read
                        if len(buffer):
                            buffer = buffer + data
                        else:
                            buffer = data
                    
                    if self._stopped:
                        break
                    
                    obj = self.deserialize_message(buffer)
                    self.on_message(obj)
                    
                except (asyncio.CancelledError, EOFError, OSError):
                    if not self._stopped:
                        self.on_error_future.set_exception(
                            Exception("Connection closed while reading from the driver")
                        )
                    break
                except Exception as e:
                    if not self._stopped:
                        self.on_error_future.set_exception(e)
                    break
                
                await asyncio.sleep(0)
            
            await self._loop.run_in_executor(None, self._proc.wait)
            
        finally:
            if not self._stopped_future.done():
                self._stopped_future.set_result(None)
    
    def _read_exact(self, n: int) -> bytes:
        assert self._proc.stdout
        data = b""
        while len(data) < n:
            chunk = self._proc.stdout.read(n - len(data))
            if not chunk:
                raise EOFError("End of file")
            data += chunk
        return data
    
    def serialize_message(self, message: Dict) -> bytes:
        msg = json.dumps(message)
        if "DEBUGP" in os.environ:
            print("\x1b[32mSEND>\x1b[0m", json.dumps(message, indent=2))
        return msg.encode()
    
    def deserialize_message(self, data: Union[str, bytes]) -> ParsedMessagePayload:
        obj = json.loads(data)
        if "DEBUGP" in os.environ:
            print("\x1b[33mRECV>\x1b[0m", json.dumps(obj, indent=2))
        return obj
    
    def send(self, message: Dict) -> None:
        assert self._output
        data = self.serialize_message(message)
        try:
            self._output.write(
                len(data).to_bytes(4, byteorder="little", signed=False) + data
            )
            self._output.flush()
        except Exception as e:
            if not self._stopped:
                self.on_error_future.set_exception(e)


def apply_patch():
    """应用补丁：替换 Playwright 的 PipeTransport"""
    try:
        import playwright._impl._transport as transport_mod
        
        transport_mod.PipeTransport = PipeTransportPatched
        
        print("[wb_playwright_transport_patch] PipeTransport 补丁已应用")
        return True
    except Exception as e:
        print(f"[wb_playwright_transport_patch] 补丁应用失败: {e}")
        return False


if __name__ == "__main__":
    apply_patch()