FROM busybox@sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213 AS toolkit

FROM ghcr.io/xtls/xray-core:26.7.11@sha256:a1644183accdb0b5be967093fe34be756fd5de15fe2ee0206e842ae17350967f

COPY --from=toolkit /bin/busybox /bin/busybox
COPY --chmod=0555 deploy/xray/supervise.sh /opt/moonli/supervise.sh
COPY --chmod=0444 deploy/xray/direct.json /opt/moonli/direct.json

USER 10001:10001

ENTRYPOINT ["/bin/busybox", "sh", "/opt/moonli/supervise.sh"]
