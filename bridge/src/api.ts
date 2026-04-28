import axios, { AxiosError } from "axios";
import FormData from "form-data";
import fs from "node:fs";
import { QueueMessage } from "./queue";
import { logger } from "./logger";

const MAX_RETRIES = 3;
const BASE_DELAY_MS = 1000;
const BACKEND_TIMEOUT_MS = Number(process.env.BACKEND_TIMEOUT_MS || 300_000);

export async function sendToBackend(msg: QueueMessage): Promise<void> {
  if (process.env.SKIP_BACKEND === "true") {
    logger.info(
      {
        sender_id: msg.senderParticipantJid || msg.senderId,
        message_type: msg.audioPath ? "audio" : "text",
      },
      "Message skipped (SKIP_BACKEND=true)"
    );
    return;
  }

  const apiUrl = process.env.API_URL || "http://localhost:8000";
  const endpoint = `${apiUrl}/process-message`;

  let lastError: Error | null = null;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      if (msg.audioPath && fs.existsSync(msg.audioPath)) {
        await sendMultipart(endpoint, msg);
      } else {
        await sendFormData(endpoint, msg);
      }

      logger.info(
        {
          sender_id: msg.senderParticipantJid || msg.senderId,
          message_type: msg.audioPath ? "audio" : "text",
        },
        "Message sent to backend"
      );
      return;
    } catch (err) {
      lastError = err as Error;
      const status = (err as AxiosError)?.response?.status;
      const errorMessage =
        (err as AxiosError)?.message || (err as Error)?.message || String(err);
      logger.warn(
        {
          sender_id: msg.senderParticipantJid || msg.senderId,
          message_type: msg.audioPath ? "audio" : "text",
        },
        `Backend request failed (attempt ${attempt}, status=${status || "n/a"}): ${errorMessage}`
      );

      if (attempt < MAX_RETRIES) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        await sleep(delay);
      }
    }
  }

  logger.error(
    {
      sender_id: msg.senderParticipantJid || msg.senderId,
      message_type: msg.audioPath ? "audio" : "text",
    },
    `All retry attempts exhausted: ${lastError?.message || String(lastError)}`
  );

  throw (lastError ?? new Error("Unknown backend processing error"));
}

async function sendFormData(endpoint: string, msg: QueueMessage): Promise<void> {
  const form = new FormData();
  form.append("sender_id", msg.senderId);
  if (msg.senderParticipantJid) {
    form.append("sender_participant_jid", msg.senderParticipantJid);
  }
  if (msg.message) {
    form.append("message", msg.message);
  }

  const response = await axios.post(endpoint, form, {
    headers: form.getHeaders(),
    timeout: BACKEND_TIMEOUT_MS,
  });

  const rawText = (response.data as { raw_text?: string } | undefined)?.raw_text;
  if (rawText) {
    logger.info(
      {
        sender_id: msg.senderParticipantJid || msg.senderId,
        message_type: "text",
      },
      "Processed transcript"
    );
  }
}

async function sendMultipart(endpoint: string, msg: QueueMessage): Promise<void> {
  const form = new FormData();
  form.append("sender_id", msg.senderId);
  if (msg.senderParticipantJid) {
    form.append("sender_participant_jid", msg.senderParticipantJid);
  }
  if (msg.message) {
    form.append("message", msg.message);
  }
  form.append("audio_file", fs.createReadStream(msg.audioPath!), {
    filename: `audio_${msg.messageId}.ogg`,
    contentType: "audio/ogg",
  });

  const response = await axios.post(endpoint, form, {
    headers: form.getHeaders(),
    timeout: BACKEND_TIMEOUT_MS,
  });

  const rawText = (response.data as { raw_text?: string } | undefined)?.raw_text;
  if (rawText) {
    logger.info(
      {
        sender_id: msg.senderParticipantJid || msg.senderId,
        message_type: "audio",
      },
      "Processed transcript"
    );
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
