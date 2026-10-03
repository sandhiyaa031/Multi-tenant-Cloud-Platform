FROM alpine:3.22
RUN apk add --no-cache pgbouncer \
    && adduser -D -H pgbouncer \
    && mkdir -p /etc/pgbouncer \
    && chown -R pgbouncer /etc/pgbouncer
COPY --chmod=755 entrypoint.sh /entrypoint.sh
# PgBouncer refuses to run as root.
USER pgbouncer
EXPOSE 6432
ENTRYPOINT ["/entrypoint.sh"]
