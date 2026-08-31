import sys
import types


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = debug
    warning = debug
    error = debug
    exception = debug


if "astrbot.api" not in sys.modules:
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot.api"] = api

