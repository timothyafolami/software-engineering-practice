# Layer 6 lab - `pricing`. Two stages so the running image is ~10MB and starts
# instantly, which matters when Topic 2 asks you to restart it repeatedly.
FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go build -o /out/pricing main.go

FROM alpine:3.21
RUN adduser -D -u 10001 pricing
COPY --from=build /out/pricing /usr/local/bin/pricing
USER pricing
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/pricing"]
