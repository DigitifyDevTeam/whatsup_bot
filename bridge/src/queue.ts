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

export class MessageQueue {
  private readonly queue: RetryableQueueMessage[] = [];
  private readonly seen: Set<string> = new Set();
  private processing = false;
  private readonly maxRetryDelayMs = 60_000;

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
      },
      "Message enqueued"
    );

    this.drain();
  }

  private async drain(): Promise<void> {
    if (this.processing) return;
    this.processing = true;

    while (this.queue.length > 0) {
      const msg = this.queue[0];
      const processed = await this.processWithInfiniteRetries(msg);
      if (processed) {
        this.queue.shift();
      }
    }

    this.processing = false;
  }

  private async processWithInfiniteRetries(msg: RetryableQueueMessage): Promise<boolean> {
    while (true) {
      try {
        await sendToBackend(msg);
        logger.info(
          {
            sender_id: msg.senderParticipantJid || msg.senderId,
            message_type: msg.audioPath ? "audio" : "text",
          },
          "Message processed successfully"
        );
        return true;
      } catch {
        msg.retryCount += 1;
        const retryDelayMs = Math.min(
          this.maxRetryDelayMs,
          2_000 * Math.pow(2, Math.max(0, msg.retryCount - 1))
        );
        logger.error(
          {
            sender_id: msg.senderParticipantJid || msg.senderId,
            message_type: msg.audioPath ? "audio" : "text",
          },
          `Message processing failed, retrying in ${retryDelayMs}ms`
        );
        await sleep(retryDelayMs);
      }
    }
  }

  get size(): number {
    return this.queue.length;
  }

  get processedCount(): number {
    return this.seen.size;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
