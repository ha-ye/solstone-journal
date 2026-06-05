// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function() {
  const CHAT_REASON_DISPLAY_NAMES = Object.freeze({
    "google": "Gemini",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "local": "Local"
  });

  const CHAT_REASONS = Object.freeze({
    "provider_key_missing": {
      "template": "{provider} needs credentials before it can read your screen descriptions",
      "action": {"label": "Open Settings", "href": "/app/settings/#providers"}
    },
    "ram_insufficient": {
      "template": "the local model needs more memory than this machine has",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "local_model_missing": {
      "template": "local model setup is not finished",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "model_missing": {
      "template": "local model setup is not finished",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "binary_missing": {
      "template": "local model setup is not finished",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "local_model_installing": {
      "template": "local model setup is finishing",
      "action": null
    },
    "local_model_loading": {
      "template": "the local model is starting up",
      "action": null
    },
    "local_model_not_ready": {
      "template": "the local model is starting up",
      "action": null
    },
    "local_server_unhealthy": {
      "template": "the local model server is not responding",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "unsupported_platform": {
      "template": "this machine is not supported for local model setup",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "unsupported_model": {
      "template": "this local model is not supported",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "sha256_mismatch": {
      "template": "local model setup could not be verified",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "archive_path_traversal": {
      "template": "local model setup could not be verified",
      "action": {"label": "Open Local Model Setup", "href": "/app/settings/#providers"}
    },
    "provider_key_invalid": {
      "template": "your {provider} key didn't validate",
      "action": {"label": "Open Settings", "href": "/app/settings/#providers"}
    },
    "provider_quota_exceeded": {
      "template": "your {provider} quota is spent — try again later",
      "action": null
    },
    "network_unreachable": {
      "template": "I couldn't reach the network",
      "action": null
    },
    "provider_response_invalid": {
      "template": "{provider}'s response didn't match the expected shape — try rephrasing or asking something more specific.",
      "action": null
    },
    "provider_unavailable": {
      "template": "{provider} is having trouble — try again",
      "action": null
    },
    "chat_pipeline_unavailable": {
      "template": "the chat pipeline isn't ready yet — try again in a moment",
      "action": null
    },
    "chat_timeout": {
      "template": "chat took too long — try again",
      "action": null
    },
    "no_output": {
      "template": "I didn't get a response — try again",
      "action": null
    },
    "unknown": {
      "template": "chat had trouble — try again",
      "action": null
    }
  });

  window.CHAT_REASON_DISPLAY_NAMES = CHAT_REASON_DISPLAY_NAMES;
  window.CHAT_REASONS = CHAT_REASONS;
  window.renderChatReason = function(code, provider) {
    const reason = CHAT_REASONS[code];
    if (!reason) {
      return {code: code, message: code, action: null};
    }
    const providerSlug = String(provider || "");
    if (code === "unknown") {
      const displayName = CHAT_REASON_DISPLAY_NAMES[providerSlug];
      const message = displayName
        ? `something went wrong with ${displayName}`
        : reason.template;
      return {code: code, message: message, action: null};
    }
    const displayName = CHAT_REASON_DISPLAY_NAMES[providerSlug] || providerSlug;
    const message = reason.template.replace(/\{provider\}/g, displayName);
    const action = reason.action
      ? {label: reason.action.label, href: reason.action.href}
      : null;
    return {code: code, message: message, action: action};
  };
})();
