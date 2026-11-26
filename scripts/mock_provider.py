"""A simulated OpenAI-compatible LLM provider for demos and in-cluster tests.

Paths: /primary/v1/chat/completions and /backup/v1/chat/completions. Returns realistic token
usage (more reasoning on the strong model, less with reasoning_effort=low), supports streaming,
and answers a share of primary calls with 429 to exercise failover (FAIL_RATE, default 0.08).

  python scripts/mock_provider.py            # listens on PORT (default 9911)
"""
import json
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider(BaseHTTPRequestHandler):
    rng = random.Random(7)
    fail_rate = float(os.getenv("FAIL_RATE", "0.08"))  # share of primary calls answered 429

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        backup = self.path.startswith("/backup")
        if not backup and self.rng.random() < self.fail_rate:
            return self._json(429, {"error": {"message": "rate limited (simulated)"}})
        prompt = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
        strong = "120b" in body["model"]
        low = body.get("reasoning_effort") == "low"
        if strong:
            reasoning = self.rng.randint(400, 2600)
        else:
            reasoning = self.rng.randint(25, 60) if low else self.rng.randint(150, 260)
        answer = self.rng.randint(120, 500) if strong else self.rng.randint(12, 40)
        cap = body.get("max_completion_tokens") or body.get("max_tokens")
        output = min(reasoning + answer, cap) if cap else reasoning + answer
        usage = {"prompt_tokens": 70 + len(prompt) // 4, "completion_tokens": output,
                 "completion_tokens_details": {"reasoning_tokens": min(reasoning, output)}}
        time.sleep((0.15 if backup else 0.05) + output / 4000)  # bigger answers take longer
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [{"choices": [{"delta": {"content": "Here is "}}]},
                      {"choices": [{"delta": {"content": "the answer."}}]},
                      {"choices": [], "usage": usage}]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return None
        return self._json(200, {"model": body["model"], "usage": usage,
                                "choices": [{"message": {"role": "assistant", "content": "Simulated answer."}}]})

    def _json(self, status, data):
        out = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> None:
    port = int(os.getenv("PORT", "9911"))
    print(f"Mock LLM provider on :{port} (fail rate {FakeProvider.fail_rate})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), FakeProvider).serve_forever()  # noqa: S104 (test server in a container)


if __name__ == "__main__":
    main()
