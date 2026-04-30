import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
  getContentType,
  WAMessage,
  proto,
  WAMessageKey,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import pino from "pino";
import path from "node:path";
import fs from "node:fs";
import qrcode from "qrcode-terminal";
import { MessageQueue, QueueMessage } from "./queue";
import { logger } from "./logger";
const silentLogger = pino({ level: "silent" });
const CP437_EXTENDED_CHARS =
  "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒ" +
  "áíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐" +
  "└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀" +
  "αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ";

export async function startWhatsApp(queue: MessageQueue): Promise<void> {
  const sessionPath = process.env.WHATSAPP_SESSION_PATH || "./auth_state";
  const processOwnMessages = process.env.PROCESS_OWN_MESSAGES === "true";
  const onlyGroupName = (process.env.ONLY_GROUP_NAME || "").trim().toLowerCase();
  const onlySenderJid = normalizeJid((process.env.ONLY_SENDER_JID || "").trim());
  const onlySenderName = (process.env.ONLY_SENDER_NAME || "").trim().toLowerCase();
  const groupNameCache = new Map<string, string>();

  if (!fs.existsSync(sessionPath)) {
    fs.mkdirSync(sessionPath, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(sessionPath);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: silentLogger as any,
    auth: {
      creds: state.creds,
      // Keep Signal/crypto internals quiet to avoid noisy session dumps.
      keys: makeCacheableSignalKeyStore(state.keys, silentLogger as any),
    },
    getMessage,
  });

  sock.ev.process(async (events) => {
    if (events["connection.update"]) {
      const { connection, lastDisconnect, qr } = events["connection.update"];

      if (qr) {
        logger.info("QR received. Scan it in WhatsApp > Linked Devices.");
        qrcode.generate(qr, { small: true });
      }

      if (connection === "close") {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        if (statusCode !== DisconnectReason.loggedOut) {
          logger.info("Connection closed, reconnecting...");
          await startWhatsApp(queue);
        } else {
          logger.warn(
            { sessionPath },
            "Logged out from WhatsApp. Clearing session and reconnecting for fresh QR."
          );
          clearSessionState(sessionPath);
          await startWhatsApp(queue);
        }
      }

      if (connection === "open") {
        logger.info("WhatsApp connection established");
      }
    }

    if (events["creds.update"]) {
      await saveCreds();
    }

    if (events["messages.upsert"]) {
      const upsert = events["messages.upsert"];
      if (upsert.type !== "notify") return;

      for (const msg of upsert.messages) {
        if (msg.key.fromMe && !processOwnMessages) continue;
        if (
          !(await shouldProcessMessage(
            msg,
            sock,
            onlyGroupName,
            onlySenderJid,
            onlySenderName,
            groupNameCache
          ))
        ) {
          continue;
        }

        try {
          logger.info(
            {
              sender_id: msg.key.participant || msg.key.remoteJid || "unknown",
              message_type: getContentType(unwrapMessage(msg.message) || {}) || "unknown",
            },
            "Incoming message captured"
          );
          await handleMessage(msg, sock, queue);
        } catch (err) {
          logger.error({ err, messageId: msg.key.id }, "Failed to handle message");
        }
      }
    }
  });

  async function getMessage(key: WAMessageKey): Promise<proto.IMessage | undefined> {
    return undefined;
  }
}

async function shouldProcessMessage(
  msg: WAMessage,
  sock: ReturnType<typeof makeWASocket>,
  onlyGroupName: string,
  onlySenderJid: string | null,
  onlySenderName: string,
  groupNameCache: Map<string, string>
): Promise<boolean> {
  const remoteJid = msg.key.remoteJid || "";
  const participantJid = msg.key.participant || "";
  const isGroupMessage = remoteJid.endsWith("@g.us");

  if (onlyGroupName) {
    if (!isGroupMessage) {
      return false;
    }

    const groupName = await getGroupName(remoteJid, sock, groupNameCache);
    if (groupName.toLowerCase() !== onlyGroupName) {
      return false;
    }
  }

  if (onlySenderJid) {
    const currentSender = normalizeJid(participantJid || remoteJid);
    if (currentSender !== onlySenderJid) {
      return false;
    }
  }

  if (onlySenderName) {
    const pushName = String((msg as any).pushName || "")
      .trim()
      .toLowerCase();
    if (!pushName) return false;
    if (pushName !== onlySenderName) return false;
  }

  return true;
}

async function getGroupName(
  groupJid: string,
  sock: ReturnType<typeof makeWASocket>,
  groupNameCache: Map<string, string>
): Promise<string> {
  const cachedGroupName = groupNameCache.get(groupJid);
  if (cachedGroupName) {
    return cachedGroupName;
  }

  try {
    const metadata = await sock.groupMetadata(groupJid);
    const subject = metadata.subject || "";
    groupNameCache.set(groupJid, subject);
    return subject;
  } catch (err) {
    logger.warn({ err, groupJid }, "Failed to resolve group name");
    return "";
  }
}

function normalizeJid(jid: string): string | null {
  if (!jid) return null;
  if (jid.includes("@")) return jid;

  const digits = jid.replace(/\D/g, "");
  if (!digits) return null;
  return `${digits}@s.whatsapp.net`;
}

async function handleMessage(
  msg: WAMessage,
  sock: ReturnType<typeof makeWASocket>,
  queue: MessageQueue
): Promise<void> {
  const messageId = msg.key.id;
  if (!messageId) return;

  const senderId = msg.key.remoteJid || "unknown";
  const senderParticipantJid = normalizeJid(msg.key.participant || "");
  const timestamp = msg.messageTimestamp
    ? Number(msg.messageTimestamp)
    : Math.floor(Date.now() / 1000);

  const normalizedMessage = unwrapMessage(msg.message);
  if (!normalizedMessage) {
    logger.info({ messageId }, "Skipping message with empty payload");
    return;
  }

  const messageType = getContentType(normalizedMessage);
  let textContent: string | null = extractTextContent(normalizedMessage);
  let audioPath: string | null = null;

  if (messageType === "audioMessage") {
    const normalizedMsg = { ...msg, message: normalizedMessage } as WAMessage;
    audioPath = await downloadAudio(normalizedMsg, sock, messageId);
  } else if (!textContent) {
    logger.info({ messageType, messageId }, "Skipping unsupported message type");
    return;
  }

  textContent = sanitizeText(textContent);

  if (!textContent && !audioPath) {
    logger.warn({ messageId }, "No usable text or audio content extracted");
    return;
  }

  if (textContent && shouldIgnoreTextMessage(textContent)) {
    logger.info({ messageId, textContent }, "Skipping short/low-signal text message");
    return;
  }

  const queueMsg: QueueMessage = {
    messageId,
    senderId,
    senderParticipantJid,
    message: textContent,
    audioPath,
    timestamp,
  };

  queue.enqueue(queueMsg);
}

function extractTextContent(message: proto.IMessage): string | null {
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    message.documentMessage?.caption ||
    message.buttonsResponseMessage?.selectedDisplayText ||
    message.listResponseMessage?.title ||
    message.templateButtonReplyMessage?.selectedDisplayText ||
    message.reactionMessage?.text ||
    null
  );
}

function sanitizeText(text: string | null): string | null {
  if (!text) return null;

  const normalized = normalizeTextEncoding(text);
  const sanitized = normalized
    .replace(/[\p{Extended_Pictographic}\p{Emoji_Presentation}]/gu, "")
    .trim();

  return sanitized.length > 0 ? sanitized : null;
}

function shouldIgnoreTextMessage(text: string | null): boolean {
  if (!text) return false;

  const normalizedText = text.trim().toLowerCase();
  if (!normalizedText) return false;
  const compactText = normalizedText.replace(/\s+/g, " ").trim();
  const thanksWords = [
    "merci",
    "merci beaucoup",
    "mrc",
    "thx",
    "thanks",
    "thank you",
    "c'est gentil",
    "c’est gentil",
    "nickel",
  ];
  const compactAlnum = compactText.replace(/[!?.;,:-]+/g, "").trim();
  if (thanksWords.includes(compactAlnum)) {
    return true;
  }
  // User requirement: any question should not become a task.
  if (text.includes("?")) {
    return true;
  }
  const questionStarters = [
    "qui",
    "que",
    "qu'est-ce que",
    "qu’est-ce que",
    "qu'est ce que",
    "qu’est ce que",
    "quoi",
    "quand",
    "ou",
    "où",
    "pourquoi",
    "comment",
    "combien",
    "est-ce que",
    "est ce que",
    "peux-tu",
    "peut-tu",
    "tu peux",
    "vous pouvez",
    "on peut",
    "dois-je",
    "doit-on",
    "is it",
    "can you",
    "could you",
  ];
  if (questionStarters.some((starter) => compactText.startsWith(`${starter} `) || compactText === starter)) {
    return true;
  }

  const ignoredWords = new Set(["ok", "bonjour", "merci", "oui", "yes", "recu", "reçu"]);
  if (ignoredWords.has(normalizedText)) {
    return true;
  }

  const tokens = normalizedText.split(/\s+/).filter(Boolean);
  if (tokens.length === 1) {
    return true;
  }

  const lowSignalPatterns: RegExp[] = [
    /^ok\s+tu\s+me\s+dis$/i,
    /^tu\s+me\s+tiens\s+au\s+courant$/i,
    /^tiens[\s-]?moi\s+au\s+courant$/i,
    /^je\s+m['’]en\s+occupe(?:\s+plus\s+tard)?(?:\s+ca\s+peut\s+tarder)?$/i,
    /^on\s+voit\s+ca\s+plus\s+tard$/i,
  ];
  if (lowSignalPatterns.some((pattern) => pattern.test(normalizedText))) {
    return true;
  }

  if (
    tokens.length <= 8 &&
    (normalizedText.includes("plus tard") ||
      normalizedText.includes("au courant") ||
      normalizedText.includes("tu me dis"))
  ) {
    return true;
  }

  return false;
}

function normalizeTextEncoding(text: string): string {
  const original = text.normalize("NFC");
  const replaced = applyMojibakeReplacements(original);
  if (!looksLikeMojibake(replaced)) {
    return replaced;
  }

  const latin1Repaired = Buffer.from(replaced, "latin1")
    .toString("utf8")
    .normalize("NFC");
  const cp437Repaired = decodeCp437Mojibake(replaced)?.normalize("NFC");
  const latin1FromOriginal = Buffer.from(original, "latin1").toString("utf8").normalize("NFC");
  const cp437FromOriginal = decodeCp437Mojibake(original)?.normalize("NFC");

  const candidates = [
    replaced,
    latin1Repaired,
    cp437Repaired,
    latin1FromOriginal,
    cp437FromOriginal,
  ].filter(
    (candidate): candidate is string => Boolean(candidate)
  );

  let best = replaced;
  let bestMojibakeCount = countMojibakeTokens(replaced);
  let bestReadableCount = countReadableChars(replaced);

  for (const candidate of candidates) {
    const mojibakeCount = countMojibakeTokens(candidate);
    const readableCount = countReadableChars(candidate);
    const isBetter =
      mojibakeCount < bestMojibakeCount ||
      (mojibakeCount === bestMojibakeCount && readableCount > bestReadableCount);

    if (isBetter) {
      best = candidate;
      bestMojibakeCount = mojibakeCount;
      bestReadableCount = readableCount;
    }
  }

  return best;
}

function looksLikeMojibake(text: string): boolean {
  return /(?:Ã.|Â.|â.|ÔÇ|├|┬)/.test(text);
}

function scoreReadability(text: string): number {
  const mojibakeHits = (text.match(/(?:Ã.|Â.|â.|ÔÇ|├|┬)/g) || []).length;
  const readableChars = (text.match(/[\p{L}\p{N}\p{P}\p{Zs}]/gu) || []).length;
  return readableChars - mojibakeHits * 4;
}

function countMojibakeTokens(text: string): number {
  return (text.match(/(?:Ã.|Â.|â.|ÔÇ|├|┬)/g) || []).length;
}

function countReadableChars(text: string): number {
  return (text.match(/[\p{L}\p{N}\p{P}\p{Zs}]/gu) || []).length;
}

function applyMojibakeReplacements(text: string): string {
  const replacements: Record<string, string> = {
    "\u00d4\u00c7\u00d6": "\u2019",
    "\u00d4\u00c7\u00a3": "\u201c",
    "\u00d4\u00c7\u00a5": "\u201d",
    "\u00d4\u00c7\u00f4": "\u2013",
    "\u00d4\u00c7\u00f6": "\u2014",
    "\u00d4\u00c7\u00aa": "\u2026",
    "\u00d4\u00c7\u00f3": "\u2022",
    "\u00d4\u00e9\u00bc": "\u20ac",
    "\u00c3\u00a9": "\u00e9",
    "\u00c3\u00a8": "\u00e8",
    "\u00c3\u00aa": "\u00ea",
    "\u00c3\u00a0": "\u00e0",
    "\u00c3\u00a2": "\u00e2",
    "\u00c3\u00a7": "\u00e7",
    "\u00c3\u00b9": "\u00f9",
    "\u00c3\u00bb": "\u00fb",
    "\u00c3\u00b4": "\u00f4",
    "\u00c3\u00ae": "\u00ee",
    "\u00c3\u00af": "\u00ef",
    "\u00c3\u00ab": "\u00eb",
    "\u251c\u00ae": "\u00e9",
    "\u251c\u00bf": "\u00e8",
    "\u251c\u00ac": "\u00ea",
    "\u251c\u00e1": "\u00e0",
    "\u251c\u00a2": "\u00e2",
    "\u251c\u00e7": "\u00e7",
    "\u251c\u2563": "\u00f9",
    "\u251c\u2557": "\u00fb",
    "\u251c\u2524": "\u00f4",
    "\u251c\u00ab": "\u00eb",
    "\u251c\u00bb": "\u00fb",
  };

  let repaired = text;
  for (const [broken, fixed] of Object.entries(replacements)) {
    repaired = repaired.split(broken).join(fixed);
  }
  return repaired;
}

function decodeCp437Mojibake(text: string): string | null {
  const bytes: number[] = [];

  for (const char of text) {
    const codePoint = char.codePointAt(0);
    if (codePoint === undefined) return null;

    if (codePoint <= 0x7f) {
      bytes.push(codePoint);
      continue;
    }

    const mappedIndex = CP437_EXTENDED_CHARS.indexOf(char);
    if (mappedIndex !== -1) {
      bytes.push(0x80 + mappedIndex);
      continue;
    }

    if (codePoint <= 0xff) {
      bytes.push(codePoint);
      continue;
    }

    return null;
  }

  return Buffer.from(bytes).toString("utf8");
}

function unwrapMessage(message: proto.IMessage | null | undefined): proto.IMessage | undefined {
  if (!message) return undefined;

  let current: proto.IMessage | undefined = message;
  while (current) {
    if (current.ephemeralMessage?.message) {
      current = current.ephemeralMessage.message;
      continue;
    }
    if (current.viewOnceMessage?.message) {
      current = current.viewOnceMessage.message;
      continue;
    }
    if (current.viewOnceMessageV2?.message) {
      current = current.viewOnceMessageV2.message;
      continue;
    }
    if (current.viewOnceMessageV2Extension?.message) {
      current = current.viewOnceMessageV2Extension.message;
      continue;
    }
    if (current.deviceSentMessage?.message) {
      current = current.deviceSentMessage.message;
      continue;
    }
    break;
  }

  return current;
}

async function downloadAudio(
  msg: WAMessage,
  sock: ReturnType<typeof makeWASocket>,
  messageId: string
): Promise<string | null> {
  try {
    const buffer = await downloadMediaMessage(msg, "buffer", {}, {
      logger: pino({ level: "silent" }) as any,
      reuploadRequest: sock.updateMediaMessage,
    });

    const audioDir = path.resolve(process.cwd(), "audio_files");
    if (!fs.existsSync(audioDir)) {
      fs.mkdirSync(audioDir, { recursive: true });
    }

    const filePath = path.join(audioDir, `audio_${messageId}.ogg`);
    fs.writeFileSync(filePath, buffer);

    logger.info({ filePath, size: buffer.length }, "Audio downloaded");
    return filePath;
  } catch (err) {
    logger.error({ err, messageId }, "Failed to download audio");
    return null;
  }
}

function clearSessionState(sessionPath: string): void {
  try {
    fs.rmSync(sessionPath, { recursive: true, force: true });
  } catch (err) {
    logger.error({ err, sessionPath }, "Failed to clear WhatsApp session folder");
  }
}
