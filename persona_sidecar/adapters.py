import json
import os


class DeliveryAdapter:
    """Delivery contract: implementations must deduplicate the idempotency key."""

    def send(self, *, persona_id, message_id, burst_index, text, idempotency_key, observed_ip):
        raise NotImplementedError


class WebhookAdapter(DeliveryAdapter):
    """Reference HTTP adapter. Receiver must honor Idempotency-Key for exactly-once effects."""

    def __init__(self, settings, egress):
        self.url = settings["url"]
        self.timeout = float(settings.get("timeout_seconds", 10))
        self.auth_token = os.environ.get(settings.get("auth_token_env", ""), "") if settings.get("auth_token_env") else ""
        self.egress = egress

    def send(self, *, persona_id, message_id, burst_index, text, idempotency_key, observed_ip):
        payload = json.dumps({
            "persona_id": persona_id,
            "message_id": message_id,
            "burst_index": burst_index,
            "text": text,
            "observed_ip": observed_ip,
        }, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        if self.auth_token:
            headers["Authorization"] = "Bearer " + self.auth_token
        status, response_body, _ = self.egress.request(
            self.url, method="POST", body=payload, headers=headers, timeout=self.timeout,
        )
        if not 200 <= status < 300:
            excerpt = response_body[:240].decode("utf-8", errors="replace")
            raise RuntimeError(f"webhook returned HTTP {status}: {excerpt}")
        return {"status": status}
