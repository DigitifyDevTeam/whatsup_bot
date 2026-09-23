import fs from "node:fs";
import { sendToBackend } from "./api";
import { logger } from "./logger";

export interface QueueMessage {
  messageId: string;
  senderId: string;
  senderParticipantJid: string | null;
  message: string | null;
  audioPath: string | null;
  timestamp: number;
}

interface RetryableQueueMessage extends QueueMessage {
  retryCount: number;
}

/** Outer queue retries after sendToBackend already exhausted its own attempts. */
const MAX_OUTER_RETRIES = Number(process.env.QUEUE_MAX_RETRIES || "8");

export class MessageQueue {
  private readonly queue: RetryableQueueMessage[] = [];
  private readonly seen: Set<string> = new Set();
  private processing = false;
  private readonly maxRetryDelayMs = 60_000;
  private readonly maxOuterRetries = Math.max(1, MAX_OUTER_RETRIES);

  enqueue(msg: QueueMessage): void {
    if (this.seen.has(msg.messageId)) {
      logger.info(
        {
          sender_id: msg.senderParticipantJid || msg.senderId,
          message_type: msg.audioPath ? "audio" : "text",
        },
        "Duplicate message, skipping"
      );
      return;
    }

    this.seen.add(msg.messageId);
    this.queue.push({ ...msg, retryCount: 0 });
    logger.info(
      {
        sender_id: msg.senderParticipantJid || msg.senderId,
        message_type: msg.audioPath ? "audio" : "text",
        queue_size: this.queue.length,
      },
      "Message enqueued"
    );

    void this.drain();
  }

  private async drain(): Promise<void> {
    if (this.processing) return;
    this.processing = true;

    while (this.queue.length > 0) {
      const msg = this.queue[0];
      await this.processOrDrop(msg);
      this.queue.shift();
      cleanupAudio(msg.audioPath);
    }

    this.processing = false;
  }

  /**
   * Retry a bounded number of times, then drop so later messages are not blocked.
   * Infinite retries previously created a permanent head-of-line stall.
   */
  private async processOrDrop(msg: RetryableQueueMessage): Promise<void> {
    while (msg.retryCount < this.maxOuterRetries) {
      try {
        await sendToBackend(msg);
        logger.info(
          {
            sender_id: msg.senderParticipantJid || msg.senderId,
            message_type: msg.audioPath ? "audio" : "text",
            queue_size: this.queue.length - 1,
          },
          "Message processed successfully"
        );
        return;
      } catch {
        msg.retryCount += 1;
        if (msg.retryCount >= this.maxOuterRetries) {
          break;
        }
        const retryDelayMs = Math.min(
          this.maxRetryDelayMs,
          2_000 * Math.pow(2, Math.max(0, msg.retryCount - 1))
        );
        logger.error(
          {
            sender_id: msg.senderParticipantJid || msg.senderId,
            message_type: msg.audioPath ? "audio" : "text",
            retry_count: msg.retryCount,
            max_retries: this.maxOuterRetries,
            queue_size: this.queue.length,
          },
          `Message processing failed, retrying in ${retryDelayMs}ms`
        );
        await sleep(retryDelayMs);
      }
    }

    logger.error(
      {
        sender_id: msg.senderParticipantJid || msg.senderId,
        message_type: msg.audioPath ? "audio" : "text",
        retry_count: msg.retryCount,
        max_retries: this.maxOuterRetries,
        queue_size: Math.max(0, this.queue.length - 1),
      },
      "Message dropped after max retries (continuing queue)"
    );
  }

  get size(): number {
    return this.queue.length;
  }

  get processedCount(): number {
    return this.seen.size;
  }
}

function cleanupAudio(audioPath: string | null): void {
  if (!audioPath) return;
  try {
    if (fs.existsSync(audioPath)) {
      fs.unlinkSync(audioPath);
    }
  } catch {
    // Best-effort cleanup; do not block the queue.
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
