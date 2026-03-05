#!/usr/bin/env python3
"""
Purge active messages from an Azure Service Bus topic and all its subscriptions.
Removes messages from both main subscription queues and dead-letter queues.
"""

import argparse
import os

from dotenv import load_dotenv

from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode, ServiceBusSubQueue
from azure.servicebus.management import ServiceBusAdministrationClient

load_dotenv()

SERVICE_BUS_CONNECTION_STR = os.getenv("SERVICE_BUS_CONNECTION")

BATCH_SIZE = 100
MAX_WAIT_SEC = 2


def purge_receiver(receiver) -> int:
    """Receive and delete messages from a subscription receiver. Returns count purged."""
    purged = 0
    while True:
        messages = receiver.receive_messages(
            max_message_count=BATCH_SIZE,
            max_wait_time=MAX_WAIT_SEC,
        )
        if not messages:
            break
        purged += len(messages)
    return purged


def purge_topic(topic_name: str, dry_run: bool = False) -> None:
    """Purge all active messages from the topic's subscriptions and their DLQs."""
    if not SERVICE_BUS_CONNECTION_STR:
        raise ValueError("SERVICE_BUS_CONNECTION environment variable is not set.")

    with ServiceBusAdministrationClient.from_connection_string(SERVICE_BUS_CONNECTION_STR) as admin:
        subscriptions = list(admin.list_subscriptions(topic_name))
        if not subscriptions:
            print(f"No subscriptions found for topic '{topic_name}'")
            return

    print(f"Topic '{topic_name}' has {len(subscriptions)} subscription(s):")
    for sub in subscriptions:
        print(f"  - {sub.name}")

    if dry_run:
        print("\n[DRY RUN] Would purge messages from the above. Run without --dry-run to purge.")
        return

    print("\nPurging active messages...")
    total_purged = 0

    with ServiceBusClient.from_connection_string(SERVICE_BUS_CONNECTION_STR) as client:
        for sub in subscriptions:
            sub_name = sub.name

            # Purge main subscription queue
            receiver = client.get_subscription_receiver(
                topic_name=topic_name,
                subscription_name=sub_name,
                receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
            )
            with receiver:
                count = purge_receiver(receiver)
                total_purged += count
                if count > 0:
                    print(f"  {sub_name}: purged {count} message(s)")

            # Purge dead-letter queue
            dlq_receiver = client.get_subscription_receiver(
                topic_name=topic_name,
                subscription_name=sub_name,
                sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE,
            )
            with dlq_receiver:
                dlq_count = purge_receiver(dlq_receiver)
                total_purged += dlq_count
                if dlq_count > 0:
                    print(f"  {sub_name}/DeadLetter: purged {dlq_count} message(s)")

    print(f"\nDone. Total messages purged: {total_purged}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Purge active messages from an Azure Service Bus topic and all its subscriptions"
    )
    parser.add_argument(
        "-t",
        "--topic",
        default=os.getenv("TOPIC_NAME", "osw-validation-scalar-request"),
        help="Topic name (default: TOPIC_NAME env or osw-validation-scalar-request)",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="List subscriptions only, do not purge",
    )
    args = parser.parse_args()

    purge_topic(args.topic, dry_run=args.dry_run)
