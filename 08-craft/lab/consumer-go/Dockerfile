FROM golang:1.24-alpine AS build
WORKDIR /src
COPY . .
RUN go build -o /consumer-go ./...
FROM alpine:3.20
COPY --from=build /consumer-go /consumer-go
EXPOSE 8080
CMD ["/consumer-go"]
