// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const TALENT_LABELS = {
    "read": {
      "running": "Reading your journal…",
      "finished": "Read your journal",
      "errored": "Couldn't finish reading your journal"
    },
    "exec": {
      "running": "Making that change…",
      "finished": "Made the change",
      "errored": "Couldn't finish the change"
    },
    "support": {
      "running": "Reaching solstone support…",
      "finished": "Reached solstone support",
      "errored": "Couldn't reach solstone support"
    }
  };

  function talentLabel(target, status) {
    const row = TALENT_LABELS[target];
    if (!row || !(status in row)) {
      throw new Error("no chat talent label for target=" + target + " status=" + status);
    }
    return row[status];
  }

  window.solChatCopy = {
    talentLabel,
    CHAT_QUEUE_INDICATOR_SINGULAR: "1 message waiting",
    CHAT_QUEUE_INDICATOR_PLURAL_FORMAT: "{count} messages waiting",
    CHAT_QUEUE_DEPTH_CAP_MESSAGE: "Give sol a moment to catch up — you have 10 messages waiting.",
    CHAT_LIVENESS_THINKING: "sol is thinking…",
    CHAT_LIVENESS_TASK_FORMAT: "{label} {task}",
    CHAT_LIVENESS_SUPPORT: "reaching solstone support on your behalf…",
    CHAT_CAPACITY_SUPPORT_ROUTE_FROM: "sol",
    CHAT_CAPACITY_SUPPORT_ROUTE_TO: "solstone support",
    CHAT_CAPACITY_SUPPORT_SUB: "reaching out on your behalf · nothing leaves without your ok",
    CHAT_OFFER_SUPPORT_YES: "yes, get support",
    CHAT_OFFER_SUPPORT_NO: "not now",
    CHAT_OFFER_SUPPORT_FALLBACK: "want me to bring in solstone support?",
    CHAT_CLOSER_LOOP_EXHAUSTED_PREFIX: "Here's what I have so far:",
    CHAT_CLOSER_DIFFERENT_ANGLE_SUFFIX: "Want me to try a different angle?",
    CHAT_CLOSER_TALENT_ERRORED_FORMAT: "I couldn't finish that lookup — {reason}. Want to try a different angle, or rephrase the question?",
    CHAT_CLOSER_TALENT_ERRORED_GENERIC: "I couldn't finish that lookup. Want to try a different angle, or rephrase the question?",
    CHAT_THINKING_EXPANDER_LABEL: "Show thinking",
    CHAT_THINKING_COLLAPSER_LABEL: "Hide thinking",
    CHAT_ERROR_DETAIL_EXPANDER_LABEL: "Show details",
    CHAT_ERROR_DETAIL_COLLAPSER_LABEL: "Hide details",
    CHAT_THINKING_SETTING_LABEL: "Thinking surfaces",
    CHAT_THINKING_OPT_ON_TAP: "Show on tap",
    CHAT_THINKING_OPT_ALWAYS: "Always show",
    CHAT_THINKING_OPT_NEVER: "Never show",
    CHAT_THINKING_SETTING_HELP: "sol does some thinking before replying. Choose how much you want to see.",
  };
})();
