#!/usr/bin/env python3
"""
Python workload that:

  - Connects to the node’s JSON-RPC interface.
  - Logs chain_id and current block number.
  - Logs the balance of the prefunded account
    0x62358b29b9e3e70ff51D88766e41a339D3e8FFff.
  - Generates configurable transaction load with adjustable TPS and concurrency.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Final

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from web3 import Web3
from web3.exceptions import Web3Exception


LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("workload")


METRIC_TPS = Gauge(
    "geth_workload_tps",
    "Achieved transactions per second over the test window",
)
METRIC_RPS = Gauge(
    "geth_workload_rpc_rps",
    "Approximate JSON-RPC requests per second over the test window",
)
METRIC_MGAS = Gauge(
    "geth_workload_mgas_per_sec",
    "Achieved mega-gas per second (MGas/s) over the test window",
)
METRIC_FAILURE_RATE = Gauge(
    "geth_workload_failure_rate",
    "Transaction failure rate over the test window (0-1)",
)
METRIC_LATENCY = Gauge(
    "geth_workload_avg_latency_seconds",
    "Average transaction latency in seconds over the test window",
)

LATENCY_HISTOGRAM = Histogram(
    "geth_workload_tx_latency_seconds",
    "Histogram of transaction latency in seconds",
)

RPC_ERROR_COUNTER = Counter(
    "geth_workload_rpc_errors_total",
    "Total JSON-RPC errors observed by the workload",
)
HEAD_BLOCK = Gauge(
    "geth_workload_head_block_number",
    "Latest block number observed by the workload",
)


def get_geth_url() -> str:
    """
    Return the Geth JSON-RPC URL from environment or use a sensible default.

    In-cluster (Kubernetes) default:
        http://geth-dev.default.svc.cluster.local:8545

    Local default (port-forwarded):
        http://localhost:8545
    """
    return os.getenv(
        "GETH_URL",
        "http://geth-dev.default.svc.cluster.local:8545",
    )


def _run_load(
    w3: Web3,
    from_address: str,
    to_address: str,
    target_tps: int,
    concurrency: int,
    duration_seconds: int,
) -> None:
    """
    Generate transaction load with the given TPS and concurrency.

    Each worker thread sends simple value transfers in a loop, pacing itself
    to achieve approximately target_tps across all workers.
    """
    if target_tps <= 0 or concurrency <= 0:
        logger.info("TARGET_TPS and CONCURRENCY must be > 0; skipping load generation.")
        return

    per_worker_tps = target_tps / float(concurrency)
    if per_worker_tps <= 0:
        logger.info(
            "TARGET_TPS=%s too low for CONCURRENCY=%s, using 1 TPS per worker.",
            target_tps,
            concurrency,
        )
        per_worker_tps = 1.0

    interval = 1.0 / per_worker_tps
    stop_at = time.time() + duration_seconds

    logger.info(
        "Starting load: target_tps=%s, concurrency=%s, per_worker_tps=%.2f, "
        "interval=%.3fs, duration=%ss",
        target_tps,
        concurrency,
        per_worker_tps,
        interval,
        duration_seconds,
    )

    total_sent = 0
    total_failed = 0
    total_gas_used = 0
    latencies: list[float] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        nonlocal total_sent, total_failed, total_gas_used
        while time.time() < stop_at:
            start = time.perf_counter()
            try:
                # Each tx will use 2–3 RPCs (gas_price, send_transaction, receipt)
                tx = {
                    "from": from_address,
                    "to": to_address,
                    "value": w3.to_wei(0.01, "ether"),
                    "gas": 21000,
                    "gasPrice": w3.eth.gas_price,
                }

                tx_hash = w3.eth.send_transaction(tx)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

                duration = time.perf_counter() - start
                gas_used = receipt.get("gasUsed", 0) or 0

                # Record successful latency in the histogram
                LATENCY_HISTOGRAM.observe(duration)

                with lock:
                    total_sent += 1
                    if receipt.get("status", 0) != 1:
                        total_failed += 1
                    total_gas_used += gas_used
                    latencies.append(duration)

                logger.debug(
                    "Worker %s: tx %s in block %s",
                    worker_id,
                    tx_hash.hex(),
                    receipt.get("blockNumber"),
                )
            except Exception as exc:  # noqa: BLE001
                with lock:
                    total_failed += 1
                RPC_ERROR_COUNTER.inc()
                logger.warning("Worker %s: tx failed: %s", worker_id, exc)

            # Pace to target TPS
            elapsed = time.perf_counter() - start
            sleep_for = interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(concurrency)
    ]

    start_time = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    end_time = time.time()

    duration = end_time - start_time
    total_tx = total_sent + total_failed
    tps = (total_tx / duration) if duration > 0 else 0.0

    # Approximate RPC calls: gas_price + send_tx + receipt per attempt
    total_rpc = total_tx * 3
    rps = (total_rpc / duration) if duration > 0 else 0.0

    mgas_per_sec = (total_gas_used / 1_000_000.0 / duration) if duration > 0 else 0.0
    failure_rate = (total_failed / total_tx) if total_tx > 0 else 0.0

    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

    logger.info(
        "Load generation finished: sent=%s, failed=%s, duration=%.2fs",
        total_sent,
        total_failed,
        duration,
    )
    logger.info(
        "Metrics: TPS=%.2f, RPC_RPS=%.2f, MGas/s=%.4f, "
        "failure_rate=%.2f%%, avg_latency=%.3fs",
        tps,
        rps,
        mgas_per_sec,
        failure_rate * 100.0,
        avg_latency,
    )

    # Export metrics for Prometheus / Grafana
    METRIC_TPS.set(tps)
    METRIC_RPS.set(rps)
    METRIC_MGAS.set(mgas_per_sec)
    METRIC_FAILURE_RATE.set(failure_rate)
    METRIC_LATENCY.set(avg_latency)


def main() -> int:
    """
    Connect to the Geth JSON-RPC endpoint and print basic information.

    Returns:
        Exit code: 0 on success, non-zero on failure.
    """
    geth_url = get_geth_url()
    logger.info("Connecting to Geth JSON-RPC at %s", geth_url)

    try:
        w3 = Web3(Web3.HTTPProvider(geth_url))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to create Web3 HTTP provider: %s", exc)
        return 1

    try:
        if not w3.is_connected():
            logger.error("Web3 is not connected to %s", geth_url)
            return 1

        chain_id = w3.eth.chain_id
        block_number = w3.eth.block_number

        logger.info("Connected to chain_id=%s", chain_id)
        logger.info("Current block number: %s", block_number)
        # export current head block for Prometheus / Grafana
        try:
            HEAD_BLOCK.set(float(block_number))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to set head block metric: %s", exc)

        # Start Prometheus metrics server (for Grafana / Prometheus scraping)
        metrics_port = int(os.getenv("METRICS_PORT", "8000"))
        start_http_server(metrics_port)
        logger.info("Prometheus metrics server started on port %s", metrics_port)

        # Background updater for head block metric so rate() works
        def _update_head_block_metric() -> None:
            while True:
                try:
                    current_block = w3.eth.block_number
                    HEAD_BLOCK.set(float(current_block))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to refresh head block metric: %s", exc)
                time.sleep(5)

        threading.Thread(target=_update_head_block_metric, daemon=True).start()

        # Check balance of the required prefunded account, if present
        target_account = "0x62358b29b9e3e70ff51D88766e41a339D3e8FFff"
        try:
            balance_wei = w3.eth.get_balance(target_account)
            balance_eth = w3.from_wei(balance_wei, "ether")
            logger.info(
                "Prefunded account %s balance: %s ETH (raw: %s wei)",
                target_account,
                balance_eth,
                balance_wei,
            )
        except Web3Exception as exc:
            logger.warning(
                "Failed to query balance for %s: %s", target_account, exc
            )

        # Load generation configuration (loop forever in a Deployment)
        while True:
            target_tps = int(os.getenv("TARGET_TPS", "0"))
            concurrency = int(os.getenv("CONCURRENCY", "0"))
            duration_seconds = int(os.getenv("DURATION_SECONDS", "60"))

            if target_tps > 0 and concurrency > 0:
                accounts = w3.eth.accounts
                if not accounts:
                    logger.error(
                        "No accounts available from eth_accounts; cannot generate load."
                    )
                    time.sleep(10)
                    continue

                from_address = accounts[0]
                logger.info(
                    "Starting load generation: TARGET_TPS=%s, CONCURRENCY=%s, DURATION_SECONDS=%s",
                    target_tps,
                    concurrency,
                    duration_seconds,
                )
                _run_load(
                    w3=w3,
                    from_address=from_address,
                    to_address=target_account,
                    target_tps=target_tps,
                    concurrency=concurrency,
                    duration_seconds=duration_seconds,
                )
            else:
                logger.info(
                    "TARGET_TPS and/or CONCURRENCY not set (>0); skipping load generation cycle."
                )

            # Short pause between cycles to avoid hammering the node config changes
            time.sleep(5)
    except Web3Exception as exc:
        logger.error("Web3 exception while talking to Geth: %s", exc)
        # In a Deployment, sleep and retry instead of exiting
        time.sleep(10)
        return main()
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error: %s", exc)
        time.sleep(10)
        return main()


if __name__ == "__main__":
    raise SystemExit(main())