// Layer 1 - Blocking vs non-blocking IO. Node's net sockets are ALWAYS
// non-blocking/event-driven under the hood (libuv registers them with
// epoll/kqueue for you; there is no synchronous socket API in normal use).
// So the "serial" case below isn't OS-level blocking IO the way Python's
// or Go's is -- it's US choosing to await one request before starting the
// next. That distinction is worth sitting with: in Node, "serial vs
// concurrent" is a decision your code makes about *scheduling*, not
// something the IO layer forces on you the way a blocking recv() does.

const net = require("net");

const RESPONSE_DELAY = 100;
const N = 20;

function startServer() {
  return new Promise((resolve) => {
    const server = net.createServer((socket) => {
      socket.on("data", () => {
        setTimeout(() => socket.end("ok"), RESPONSE_DELAY);
      });
    });
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function request(port) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(port, "127.0.0.1", () => {
      socket.write("ping");
    });
    socket.on("data", () => {
      socket.end();
      resolve();
    });
    socket.on("error", reject);
  });
}

async function benchSerial(port) {
  const start = Date.now();
  for (let i = 0; i < N; i++) {
    await request(port); // deliberately not starting the next until this resolves
  }
  return (Date.now() - start) / 1000;
}

async function benchConcurrent(port) {
  const start = Date.now();
  await Promise.all(Array.from({ length: N }, () => request(port)));
  return (Date.now() - start) / 1000;
}

(async () => {
  const { server, port } = await startServer();
  const tSerial = await benchSerial(port);
  const tConcurrent = await benchConcurrent(port);
  console.log(`N=${N} requests, ${RESPONSE_DELAY}ms server delay each`);
  console.log(`"serial" (awaited one at a time): ${tSerial.toFixed(3)}s  (~${((tSerial / N) * 1000).toFixed(0)}ms/req)`);
  console.log(`concurrent (Promise.all):         ${tConcurrent.toFixed(3)}s`);
  server.close();
})();
