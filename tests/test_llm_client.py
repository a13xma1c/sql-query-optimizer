"""Local-LLM transport and answer parsing (no network: stub transports + a local stub server)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from sqlanalyzer.llm import OLLAMA, OPENAI, LlmClient, LlmConfig, LlmError, build_prompt, parse_response
from sqlanalyzer.llm.client import DEFAULT_URLS, http_json
from sqlanalyzer.models import Category, Finding, Severity
from sqlanalyzer.optimizer import optimize_query


class Recorder:
    """Stub transport that records the calls and replays canned answers."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict | None, dict, float]] = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


# ------------------------------------------------------------------------ config
def test_config_defaults_and_display():
    cfg = LlmConfig()
    assert cfg.provider == OLLAMA and cfg.base_url == DEFAULT_URLS[OLLAMA] and cfg.temperature == 0.0
    assert "@" in cfg.display() and cfg.model in cfg.display()
    assert LlmConfig(base_url="http://host:1234/").normalized_url() == "http://host:1234"
    assert LlmConfig(base_url="").normalized_url() == DEFAULT_URLS[OLLAMA]


# ------------------------------------------------------------------- completions
def test_ollama_request_shape_and_answer():
    transport = Recorder({"message": {"role": "assistant", "content": "SELECT 1"}})
    client = LlmClient(LlmConfig(model="codellama", temperature=0.2, max_tokens=99), transport)
    assert client.complete("rewrite this", "be terse") == "SELECT 1"
    url, payload, headers, timeout = transport.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert payload["model"] == "codellama" and payload["stream"] is False
    assert payload["options"] == {"temperature": 0.2, "num_predict": 99}
    assert payload["messages"] == [{"role": "system", "content": "be terse"},
                                   {"role": "user", "content": "rewrite this"}]
    assert headers == {} and timeout == 120.0


def test_ollama_generate_style_answer_is_accepted():
    client = LlmClient(LlmConfig(), Recorder({"response": "SELECT 2"}))
    assert client.complete("x") == "SELECT 2"


def test_openai_request_shape_and_api_key():
    transport = Recorder({"choices": [{"message": {"content": "SELECT 3"}}]})
    cfg = LlmConfig(provider=OPENAI, base_url="http://localhost:8080", model="local-model", api_key="secret")
    assert LlmClient(cfg, transport).complete("p") == "SELECT 3"
    url, payload, headers, _ = transport.calls[0]
    assert url == "http://localhost:8080/v1/chat/completions"
    assert payload["max_tokens"] == cfg.max_tokens and payload["temperature"] == 0.0
    assert headers["Authorization"] == "Bearer secret"


def test_empty_or_unknown_answers_raise():
    with pytest.raises(LlmError, match="empty answer"):
        LlmClient(LlmConfig(), Recorder({"message": {"content": "   "}})).complete("p")
    with pytest.raises(LlmError, match="empty answer"):
        LlmClient(LlmConfig(provider=OPENAI), Recorder({"choices": []})).complete("p")
    with pytest.raises(LlmError, match="Unknown provider"):
        LlmClient(LlmConfig(provider="telepathy"), Recorder({})).complete("p")


def test_transport_errors_are_wrapped():
    client = LlmClient(LlmConfig(), Recorder(LlmError("Cannot reach http://localhost:11434: refused")))
    with pytest.raises(LlmError, match="Cannot reach"):
        client.complete("p")


# ------------------------------------------------------------------------ models
def test_model_listing_and_ping():
    ollama = LlmClient(LlmConfig(model="mistral"), Recorder({"models": [{"name": "mistral:7b"}, {"name": "phi3"}]}))
    assert ollama.models() == ["mistral:7b", "phi3"]
    assert "2 model(s) available" in ollama.ping() and "Warning" not in ollama.ping()
    missing = LlmClient(LlmConfig(model="llama3"), Recorder({"models": [{"name": "phi3"}]}))
    assert "Warning" in missing.ping() and "llama3" in missing.ping()
    empty = LlmClient(LlmConfig(), Recorder({"models": []}))
    assert "lists no models" in empty.ping()
    openai = LlmClient(LlmConfig(provider=OPENAI), Recorder({"data": [{"id": "b"}, {"id": "a"}]}))
    assert openai.models() == ["a", "b"]


# ------------------------------------------------------ the real urllib transport
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.loads(body)
        self._send({"message": {"content": f"echo:{payload['messages'][-1]['content']}"}})

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.endswith("/boom"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"kaput")
            return
        self._send({"models": [{"name": "stub-model"}]})

    def _send(self, data: dict) -> None:
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_client_talks_http_to_a_local_server(stub_server):
    client = LlmClient(LlmConfig(base_url=stub_server, model="stub-model", timeout=10))
    assert client.complete("hello", "sys") == "echo:hello"
    assert client.models() == ["stub-model"]
    assert "1 model(s) available" in client.ping()


def test_http_errors_become_llm_errors(stub_server):
    with pytest.raises(LlmError, match="HTTP 500"):
        http_json(f"{stub_server}/boom", None, {}, 5)
    with pytest.raises(LlmError, match="Cannot reach"):
        http_json("http://127.0.0.1:1/api/tags", None, {}, 2)


# ------------------------------------------------------------------------ prompt
def test_prompt_contains_the_context_the_model_needs():
    findings = [Finding("select-star", Severity.INFO, Category.LINT, "SELECT * reads every column.",
                        "List the columns.", line=1, col=8)]
    rules = optimize_query("SELECT * FROM a, b WHERE a.id = b.id", "postgres")
    prompt = build_prompt("SELECT * FROM a, b WHERE a.id = b.id", "postgres",
                          schema_text="a: id int\nb: id int", partition_text="a: dt",
                          findings=findings, rules=rules)
    assert "Engine / SQL dialect: postgres." in prompt
    assert "SELECT * FROM a, b WHERE a.id = b.id" in prompt
    assert "a: id int" in prompt and "a: dt" in prompt
    assert "line 1: [select-star]" in prompt
    assert "rule engine produced this rewrite" in prompt and "explicit-join" in prompt
    assert '"sql"' in prompt and "Never drop a filter" in prompt


def test_prompt_without_schema_warns_about_star_expansion():
    prompt = build_prompt("SELECT * FROM a", "duckdb")
    assert "No schema was declared" in prompt and "do not expand SELECT *" in prompt
    assert "rule engine produced" not in prompt


# ---------------------------------------------------------------- answer parsing
def test_parse_plain_json_answer():
    s = parse_response('{"sql": "SELECT 1", "changes": [{"title": "t", "reason": "r"}], "notes": "n"}')
    assert s.ok and s.sql == "SELECT 1" and s.change_titles() == ["t"] and s.notes == "n"
    assert not s.parse_error


def test_parse_json_in_a_fenced_block_with_chatter():
    answer = 'Sure!\n```json\n{"sql": "SELECT a FROM t", "changes": []}\n```\nHope this helps.'
    s = parse_response(answer)
    assert s.sql == "SELECT a FROM t" and s.changes == [] and s.raw == answer


def test_parse_json_with_braces_in_strings():
    s = parse_response('noise {"sql": "SELECT \'{\' AS x", "notes": "}"} trailing')
    assert s.sql == "SELECT '{' AS x" and s.notes == "}"


def test_parse_sql_code_block_fallback():
    s = parse_response("Here is a better query:\n```sql\nSELECT a FROM t WHERE b = 1;\n```")
    assert s.sql.startswith("SELECT a FROM t") and "not JSON" in s.parse_error and s.changes == []


def test_parse_bare_sql_and_failures():
    assert parse_response("WITH x AS (SELECT 1) SELECT * FROM x").ok
    bad = parse_response("I cannot help with that.")
    assert not bad.ok and bad.parse_error and bad.sql == ""
    assert not parse_response("").ok
    assert not parse_response('{"changes": []}').ok


def test_parse_normalizes_change_entries():
    s = parse_response('{"sql": "SELECT 1", "changes": ["just a string", {"title": "x"}], "notes": null}')
    assert s.change_titles() == ["just a string", "x"] and s.notes == ""
