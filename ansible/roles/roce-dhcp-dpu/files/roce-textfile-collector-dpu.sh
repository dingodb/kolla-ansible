#!/bin/bash
# /usr/local/bin/roce-textfile-collector-dpu.sh
# 由 systemd timer 每 30s 调用，把 roce_sidecar 和 doca-telemetry 的 metrics
# 写到 node_exporter textfile 目录，node_exporter 会把它合并进 :9100/metrics

OUTDIR=/var/lib/node_exporter/textfile_collector
mkdir -p "$OUTDIR"

# roce_sidecar：分配状态、DHCP 统计
TMP=$(mktemp /tmp/roce-textfile-XXXXXX.prom)
if curl -sf --max-time 5 http://127.0.0.1:9967/metrics > "$TMP"; then
    chmod 644 "$TMP"
    mv "$TMP" "$OUTDIR/roce_sidecar.prom"
else
    rm -f "$TMP" "$OUTDIR/roce_sidecar.prom"
fi

# doca-telemetry-service：IB 端口吞吐量、链路状态、BF3 硬件计数器
TMP=$(mktemp /tmp/roce-textfile-XXXXXX.prom)
if curl -sf --max-time 10 http://127.0.0.1:9101/metrics > "$TMP"; then
    chmod 644 "$TMP"
    mv "$TMP" "$OUTDIR/doca_telemetry.prom"
else
    rm -f "$TMP" "$OUTDIR/doca_telemetry.prom"
fi
