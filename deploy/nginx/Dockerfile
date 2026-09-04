FROM nginx:stable-alpine@sha256:02b1b2a0445514891a14aa371845f6085d5d9d10d385b30d6aad606a50a29a05

RUN apk add --no-cache openssl

COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY 10-moonli-certificate.sh /docker-entrypoint.d/10-moonli-certificate.sh

RUN chmod 755 /docker-entrypoint.d/10-moonli-certificate.sh
