"""Small typed PH330Lib 2.x binding and non-acquiring communication probe."""

import argparse
import ctypes as ct
import json
import os
from pathlib import Path
import sys


DEFAULT_DLL = Path(r"C:\Program Files\PicoQuant\UniHarp\PH330Lib.dll")
INT = ct.c_int
PINT = ct.POINTER(INT)
CHAR = ct.POINTER(ct.c_char)


class PH330Error(RuntimeError):
    def __init__(self, function, code, message):
        self.function = function
        self.code = code
        super().__init__(f"PH330_{function}: {message} ({code})")


class PH330:
    def __init__(self, dll=DEFAULT_DLL):
        if sys.platform != "win32" or ct.sizeof(ct.c_void_p) != 8:
            raise RuntimeError("PH330Lib requires 64-bit Windows Python.")
        self.path = Path(dll).resolve(strict=True)
        self._dll_directory = os.add_dll_directory(str(self.path.parent))
        self.dll = ct.WinDLL(str(self.path))
        signatures = {
            "GetLibraryVersion": [CHAR],
            "GetErrorString": [CHAR, INT],
            "OpenDevice": [INT, CHAR],
            "CloseDevice": [INT],
        }
        for name, signature in signatures.items():
            self.bind(name, signature)
        value = ct.create_string_buffer(8)
        self.call("GetLibraryVersion", value)
        self.version = value.value.decode("ascii")
        if self.version.split(".")[0] != "2":
            raise RuntimeError(f"Unverified PH330Lib version {self.version}; expected 2.x.")

    def bind(self, name, signature):
        function = getattr(self.dll, "PH330_" + name)
        function.argtypes = signature
        function.restype = INT

    def call(self, name, *args):
        code = getattr(self.dll, "PH330_" + name)(*args)
        if code < 0:
            text = ct.create_string_buffer(80)
            self.dll.PH330_GetErrorString(text, code)
            raise PH330Error(name, code, text.value.decode("ascii", errors="replace"))
        return code

    def probe(self):
        """Open/close each index, without initialization or input changes."""
        results = []
        for index in range(8):
            serial = ct.create_string_buffer(8)
            try:
                self.call("OpenDevice", index, serial)
            except PH330Error as exc:
                results.append({"index": index, "opened": False,
                                "error_code": exc.code, "error": str(exc)})
            else:
                try:
                    results.append({"index": index, "opened": True,
                                    "serial": serial.value.decode("ascii")})
                finally:
                    self.call("CloseDevice", index)
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--probe", action="store_true",
                        help="Open/close available devices; does not initialize or acquire")
    args = parser.parse_args(argv)
    try:
        api = PH330(args.dll)
        result = {"python": sys.executable, "dll": str(api.path),
                  "library_version": api.version}
        if args.probe:
            result["devices"] = api.probe()
        print(json.dumps(result, indent=2))
        if args.probe and not any(d["opened"] for d in result["devices"]):
            print("No device opened. Check power/USB and close UniHarp or other device software.")
            return 1
        return 0
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("Connection test")