FROM alpine:3.19
RUN apk add --no-cache tcpdump
ENTRYPOINT ["tcpdump", "-i", "any", "-s", "0", "-U", "-w"]
CMD ["/captures/output.pcap"]
