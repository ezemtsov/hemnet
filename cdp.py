"""Tiny CDP helper for driving a Chromium tab via remote debugging.

Port is read from $HEMNET_CDP_PORT (default 9222), so a second Chromium
instance for the for-sale pipeline can be driven via:

    HEMNET_CDP_PORT=9223 python3 scrape.py --url "https://www.hemnet.se/bostader?..."
"""
import json, os, urllib.request, websocket

CDP_PORT = int(os.environ.get("HEMNET_CDP_PORT", "9222"))
DEVTOOLS_URL = f"http://localhost:{CDP_PORT}"


def find_tab(url_substring: str = "hemnet.se", devtools_url: str = DEVTOOLS_URL) -> str:
    """Return the websocket URL of the first page tab matching the substring."""
    tabs = json.load(urllib.request.urlopen(f"{devtools_url}/json"))
    for t in tabs:
        if t["type"] == "page" and url_substring in t["url"]:
            return t["webSocketDebuggerUrl"]
    raise SystemExit(f"no tab found with url substring {url_substring!r}; is Chromium running on {devtools_url}?")


class CDP:
    """Minimal synchronous wrapper around a single CDP websocket session."""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, origin=DEVTOOLS_URL)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg

    def eval(self, expr: str):
        """Evaluate a JS expression and return the unwrapped result value."""
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r["result"]["result"].get("value")

    def navigate(self, url: str):
        self.call("Page.navigate", {"url": url})
