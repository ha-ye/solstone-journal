// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

(function () {
  const TALENT_LABELS = {
    "exec": {
      "running": "Looking in your journal…",
      "finished": "Looked in your journal",
      "errored": "Couldn't finish looking in your journal"
    },
    "reflection": {
      "running": "Reflecting…",
      "finished": "Reflected",
      "errored": "Couldn't finish reflecting"
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
    CHAT_QUEUE_DEPTH_CAP_MESSAGE: "Give sol a moment to catch up — you have 10 messages waiting."
  };
})();
