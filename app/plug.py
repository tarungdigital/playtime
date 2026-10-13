"""Tapo P110 control over the local network, via the `tapo` library.

The plug must be reachable from this machine (same LAN), and "Third-Party
Compatibility" must be on in the Tapo app (Me > Third-Party Services)."""

from . import config

_devices = {}  # ip -> connected handler, reused between calls
_dry_run_state = {}  # ip -> bool, used when PLUG_DRY_RUN=true
_client = None
# From .env at first; the admin panel can replace them (see set_credentials).
_username, _password = config.TAPO_USERNAME, config.TAPO_PASSWORD


def set_credentials(username: str, password: str):
    global _client, _username, _password
    _username, _password, _client = username, password, None
    _devices.clear()  # reconnect every plug with the new login


def credentials() -> tuple[str, str]:
    return _username, _password


def _api(login: tuple[str, str] | None = None):
    from tapo import ApiClient

    if login:  # one-off client, to try new details without touching the live one
        return ApiClient(*login)
    global _client
    if _client is None:
        if not (_username and _password):
            raise RuntimeError("Tapo login not set: add it in the admin panel")
        _client = ApiClient(_username, _password)
    return _client


async def _device(ip: str):
    if ip not in _devices:
        _devices[ip] = await _api().p110(ip)
    return _devices[ip]


async def identify(ip: str, login: tuple[str, str] | None = None) -> dict:
    """Ask the plug at `ip` for its name, model and state, so staff can confirm
    it's the right plug (and, with `login`, that the Tapo login works)."""
    if config.PLUG_DRY_RUN:
        return {"nickname": "Simulated plug", "model": "P110", "on": _dry_run_state.get(ip, False)}
    info = await (await _api(login).p110(ip)).get_device_info()
    return {"nickname": info.nickname, "model": info.model, "on": info.device_on}


def forget(ip: str):
    _devices.pop(ip, None)


async def is_on(ip: str) -> bool:
    if config.PLUG_DRY_RUN:
        return _dry_run_state.get(ip, False)
    try:
        info = await (await _device(ip)).get_device_info()
        return info.device_on
    except Exception:
        _devices.pop(ip, None)  # reconnect from scratch next time
        raise


async def set_power(ip: str, on: bool):
    if config.PLUG_DRY_RUN:
        _dry_run_state[ip] = on
        return
    try:
        device = await _device(ip)
        await (device.on() if on else device.off())
    except Exception:
        _devices.pop(ip, None)
        raise
