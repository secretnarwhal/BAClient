"""The "network connection is unstable" Notice. The client always reconnects.

Only the exact wording counts, and only its Reconnect is pressed. Its Confirm returns to the title and is never
pressed. Duplicate login and every other Notice are handled as before.
"""
import time

from . import watchdog

MAX_PRESSES = 6                  # in WINDOW seconds: past this the network is really down, so stop
WINDOW = 600.0
CLEAR_LOOKS = 45                 # one-second looks for the Notice to go after a press

_presses = []


class ReconnectFailed(RuntimeError):
    """Reconnect couldn't be pressed, or it didn't clear the Notice."""


def answer(game, frame=None, log=None):
    """If exactly that Notice is on top, press Reconnect and wait for it to go. False if it isn't open."""
    log = log or game.log
    frame = game.grab() if frame is None else frame
    if not watchdog.network_notice(frame):
        return False
    spot = watchdog.network_reconnect(frame)
    if spot is None:
        raise ReconnectFailed("the network Notice is open but its Reconnect button can't be confirmed")
    now = time.monotonic()
    _presses[:] = [t for t in _presses if now - t < WINDOW]
    if len(_presses) >= MAX_PRESSES:
        raise ReconnectFailed(f"pressed Reconnect {len(_presses)} times in {int(WINDOW / 60)} minutes and the network "
                              "Notice keeps coming back")
    _presses.append(now)
    log("network notice: pressing Reconnect")
    game.press_reconnect(*spot)
    for _ in range(CLEAR_LOOKS):
        game.sleep(1.0)
        if not watchdog.network_notice(game.grab()):
            log("network notice: reconnected")
            return True
    raise ReconnectFailed("the network Notice was still open after Reconnect")
