#!/bin/bash
# /usr/local/bin/roce-textfile-collector.sh
# 由 systemd timer 每 30s 调用，把 roce_sidecar 的 Prometheus metrics
# 写到 node_exporter textfile 目录，node_exporter 会把它合并进 :9100/metrics

OUTDIR=/var/lib/node_exporter/textfile_collector
mkdir -p "$OUTDIR"

TMP=$(mktemp /tmp/roce-textfile-XXXXXX.prom)
if curl -sf --max-time 5 http://127.0.0.1:9967/metrics > "$TMP"; then
    chmod 644 "$TMP"
    mv "$TMP" "$OUTDIR/roce_sidecar.prom"
else
    # 抓取失败时删掉旧文件，让 node_textfile_mtime_seconds 超时告警生效
    rm -f "$TMP" "$OUTDIR/roce_sidecar.prom"
fi
