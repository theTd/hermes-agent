function normalizeChatType(chatType: string): "dm" | "group" | null {
  if (chatType === "dm" || chatType === "private") {
    return "dm";
  }
  if (chatType === "group") {
    return "group";
  }
  return null;
}

interface SessionLabelContext {
  chatType?: string | null;
  chatId?: string | null;
  chatName?: string | null;
}

function parseNapcatSessionKey(sessionKey: string): { chatType: "dm" | "group"; chatId: string } | null {
  const parts = sessionKey.split(":").filter(Boolean);

  if (parts.length >= 5 && parts[0] === "agent" && parts[1] === "main" && parts[2] === "napcat") {
    const chatType = normalizeChatType(parts[3] || "");
    const chatId = (parts[4] || "").trim();
    if (chatType && chatId) {
      return { chatType, chatId };
    }
  }

  if (parts.length >= 3 && parts[0] === "napcat") {
    const chatType = normalizeChatType(parts[1] || "");
    const chatId = (parts[2] || "").trim();
    if (chatType && chatId) {
      return { chatType, chatId };
    }
  }

  return null;
}

export function formatSessionLabel(sessionKey: string, context?: SessionLabelContext): string {
  const parsed = parseNapcatSessionKey(sessionKey);
  if (!parsed) {
    return sessionKey;
  }

  const chatType = normalizeChatType(String(context?.chatType || parsed?.chatType || "").trim());
  const chatId = String(context?.chatId || parsed?.chatId || "").trim();
  const chatName = String(context?.chatName || "").trim();

  if (!chatType || !chatId) {
    return sessionKey;
  }

  if (chatType === "dm") {
    return `DM:${chatId}`;
  }

  if (chatName && chatName !== chatId) {
    return `Group: ${chatName} (${chatId})`;
  }

  return `Group:${chatId}`;
}
