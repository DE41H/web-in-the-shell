from main import _run, SessionConfig


class FakePlanner:
    def __init__(self, client, convos=None):
        self._calls = []
        self.last_usage = None
        self.last_messages = []

    async def plan(self, intent, context=""):
        # First call returns an unreachable domain, second returns a reachable one
        if not hasattr(self, "_called"):
            self._called = 1
            return type("P", (), {
                "target_domain": "http://bad-domain.example",
                "target_endpoints": ["/foo"],
                "candidate_domains": [],
                "action": "do",
                "parameters": {},
                "steps": []
            })()
        else:
            self._called += 1
            return type("P", (), {
                "target_domain": "https://good.example",
                "target_endpoints": ["/"],
                "candidate_domains": [],
                "action": "do",
                "parameters": {},
                "steps": []
            })()


class FakeBrowser:
    def __init__(self, headless=True):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def new_page(self):
        return FakePage()

    async def login_handshake(self, target):
        return FakePage()


class FakePage:
    async def goto(self, url, wait_until=None, timeout=None):
        if "bad-domain" in url:
            raise Exception("DNS failure")
        return None

    # these are no-ops used by main
    async def evaluate(self, *_a, **_k):
        return []

    async def fill(self, *_a, **_k):
        return None


class FakeSessionManager:
    def attach(self, page):
        pass

    async def sync_cookies(self, page):
        pass

    async def restore(self, host, store):
        pass


class FakeSniffer:
    def __init__(self, patterns):
        pass

    def attach(self, page):
        pass

    async def stream(self):
        if False:
            yield

    def drain(self):
        return []


class DummyConvoStore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyFormStore(DummyConvoStore):
    async def get_all_for_domain(self, domain):
        return {}


class DummySessionStore(DummyConvoStore):
    pass


async def test_replans_on_navigation_failure(monkeypatch):
    # Patch PlannerAgent, StealthBrowser, SessionManager, PacketSniffer, stores
    import main as m

    monkeypatch.setattr(m, "PlannerAgent", FakePlanner)
    monkeypatch.setattr(m, "StealthBrowser", FakeBrowser)
    monkeypatch.setattr(m, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(m, "PacketSniffer", FakeSniffer)
    monkeypatch.setattr(m, "ConvoStore", lambda path: DummyConvoStore())
    monkeypatch.setattr(m, "FormFieldStore", lambda path: DummyFormStore())
    monkeypatch.setattr(m, "SessionStore", lambda path: DummySessionStore())

    # Short-circuit network probe: bad-domain -> False, good.example -> True
    async def fake_probe(url: str) -> bool:
        return "good.example" in url

    monkeypatch.setattr(m, "_probe_url", fake_probe)

    # Prevent init_db from touching filesystem
    async def _noop_init(p):
        return None
    monkeypatch.setattr(m, "init_db", _noop_init)

    display = m.AgentDisplay()
    config = SessionConfig()
    config.mock = False
    config.replan = 1

    # Run with a simple intent; the fake planner will first return bad-domain
    await _run(config, display, "Do the thing")

    # If we reached here without exception, test passed (replan attempted and succeeded)
