const form = document.querySelector("#scenario");
const button = document.querySelector("#run-check");
const result = document.querySelector("#result");
const decision = document.querySelector("#decision");
const explanation = document.querySelector("#explanation");
const effectStatus = document.querySelector("#effect-status");
const error = document.querySelector("#error");

let revision = 0;
let pending;
function clearResult() {
  revision++;
  pending?.abort();
  result.className = "receipt";
  decision.textContent = "Selection changed. Run the policy check again.";
  explanation.textContent = "";
  effectStatus.textContent = "";
  error.hidden = true;
  button.disabled = false;
  button.textContent = "Run policy check";
}
form.addEventListener("change", clearResult);
document.querySelector("#holder-answer").addEventListener("change", clearResult);

const decisionLabels = {
  allowed: "Allowed by this policy",
  requires_holder: "Ask the holder first",
  forbidden: "Blocked by this policy",
};

button.addEventListener("click", async () => {
  const current = ++revision;
  pending?.abort();
  const controller = new AbortController();
  pending = controller;
  const timer = setTimeout(() => controller.abort(), 12000);
  button.disabled = true;
  button.textContent = "Checking policy...";
  error.hidden = true;
  result.className = "receipt";
  decision.textContent = "Checking the request";
  explanation.textContent = "The policy engine is evaluating the selected scenario.";
  effectStatus.textContent = "";

  try {
    const response = await fetch("/api/demo", {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario: form.value, answer: document.querySelector("#holder-answer").value }),
    });
    const payload = await response.json();
    if (current !== revision) return;
    if (!response.ok) throw new Error(payload.error || "The policy check failed.");

    result.classList.add(payload.decision);
    decision.textContent = payload.effect_written ? "Local authorization committed" : payload.reason === "holder_declined" ? "Holder declined" : payload.reason === "revoked" ? "Mandate withdrawn" : decisionLabels[payload.decision] || "Policy result returned";
    explanation.textContent = payload.refusal || "The selected rule allows this request.";
    if (payload.requires_holder) {
      explanation.textContent = "This action needs a holder decision before it can proceed.";
    }
    if (payload.question_es) explanation.textContent = `${payload.question_en} / ${payload.question_es}`;
    effectStatus.textContent = payload.effect_written
      ? `Local effect committed. Retry: ${payload.repeat_status}. Effect rows: ${payload.effect_count}. No external action occurred.`
      : `No effect committed. Reason: ${payload.reason}. Effect rows: ${payload.effect_count}.`;
  } catch (cause) {
    if (current !== revision) return;
    decision.textContent = "No decision returned";
    explanation.textContent = "The policy service did not return a result. Try again.";
    effectStatus.textContent = "";
    error.textContent = cause instanceof Error ? cause.message : "The policy check failed.";
    error.hidden = false;
  } finally {
    clearTimeout(timer);
    if (current === revision) {
      button.disabled = false;
      button.textContent = "Run policy check";
    }
  }
});
