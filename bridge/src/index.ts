import dotenv from "dotenv";
import { MessageQueue } from "./queue";
import { startWhatsApp } from "./whatsapp";
import { logger } from "./logger";

dotenv.config();

function installSignalNoiseFilter(): void {
  const patternsToIgnore = [
    "Failed to decrypt message with any known session",
    "Session error:Error: Bad MAC Error: Bad MAC",
    "Closing open session in favor of incoming prekey bundle",
    "Closing session: SessionEntry",
    String.raw`\libsignal\src\session_cipher.js`,
    String.raw`\libsignal\src\crypto.js`,
  ];

  const shouldIgnore = (args: unknown[]): boolean => {
    const combined = args
      .map((arg) => {
        if (typeof arg === "string") return arg;
        if (arg instanceof Error) return arg.stack || arg.message;
        try {
          return JSON.stringify(arg);
        } catch {
          return String(arg);
        }
      })
      .join(" ");

    return patternsToIgnore.some((pattern) => combined.includes(pattern));
  };

  const originalError = console.error.bind(console);
  const originalWarn = console.warn.bind(console);
  const originalLog = console.log.bind(console);

  console.error = (...args: unknown[]) => {
    if (shouldIgnore(args)) return;
    originalError(...args);
  };
  console.warn = (...args: unknown[]) => {
    if (shouldIgnore(args)) return;
    originalWarn(...args);
  };
  console.log = (...args: unknown[]) => {
    if (shouldIgnore(args)) return;
    originalLog(...args);
  };

  const installStreamFilter = (stream: NodeJS.WriteStream): void => {
    const originalWrite = stream.write.bind(stream) as (...args: any[]) => boolean;
    let suppressSessionDump = false;
    let braceDepth = 0;

    const resolveCallback = (encodingOrCb: unknown, maybeCb: unknown): (() => void) | undefined => {
      if (typeof encodingOrCb === "function") return encodingOrCb as () => void;
      if (typeof maybeCb === "function") return maybeCb as () => void;
      return undefined;
    };

    const toUtf8Text = (chunk: unknown): string | null => {
      if (typeof chunk === "string") return chunk;
      if (Buffer.isBuffer(chunk)) return chunk.toString("utf8");
      return null;
    };

    const filterText = (text: string): string => {
      const lines = text.split(/(\r?\n)/);
      const kept: string[] = [];

      for (let i = 0; i < lines.length; i += 2) {
        const line = lines[i] ?? "";
        const newline = lines[i + 1] ?? "";

        if (!line && !newline) continue;

        if (suppressSessionDump) {
          braceDepth += (line.match(/\{/g) || []).length;
          braceDepth -= (line.match(/\}/g) || []).length;

          if (braceDepth <= 0) {
            suppressSessionDump = false;
            braceDepth = 0;
          }
          continue;
        }

        if (line.includes("Closing session: SessionEntry")) {
          suppressSessionDump = true;
          braceDepth = (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
          if (braceDepth <= 0) {
            suppressSessionDump = false;
            braceDepth = 0;
          }
          continue;
        }

        if (patternsToIgnore.some((pattern) => line.includes(pattern))) {
          continue;
        }

        kept.push(line + newline);
      }

      return kept.join("");
    };

    (stream as any).write = (...args: any[]): boolean => {
      const [chunk, encodingOrCb, maybeCb] = args;
      const callback = resolveCallback(encodingOrCb, maybeCb);
      const text = toUtf8Text(chunk);

      if (text === null) {
        return originalWrite(...args);
      }

      const filteredText = filterText(text);
      if (!filteredText) {
        if (callback) callback();
        return true;
      }

      if (typeof encodingOrCb === "function") {
        return originalWrite(filteredText, callback);
      }

      return originalWrite(filteredText, encodingOrCb, callback);
    };
  };

  installStreamFilter(process.stdout);
  installStreamFilter(process.stderr);
}

async function main(): Promise<void> {
  installSignalNoiseFilter();
  logger.info("Starting WhatsApp-to-Teamwork bridge...");
  const skipBackend = process.env.SKIP_BACKEND === "true";
  logger.info(
    {
      apiUrl: process.env.API_URL || "http://localhost:8000",
      skipBackend,
      processOwnMessages: process.env.PROCESS_OWN_MESSAGES === "true",
    },
    "Bridge configuration"
  );

  const queue = new MessageQueue();
  await startWhatsApp(queue);

  logger.info("Bridge is running. Scan QR code if this is a first connection.");
}

main().catch((err) => {
  logger.fatal({ err }, "Bridge crashed");
  process.exit(1);
});
