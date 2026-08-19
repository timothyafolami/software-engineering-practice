// Layer 1 - Blocking vs non-blocking IO, Java version.
// java.net.Socket is genuinely blocking, same story as Python's raw
// sockets or C++'s std::net. java.nio's SocketChannel + Selector is Java's
// direct abstraction over the OS readiness API -- on Linux, java.nio's
// default SelectorProvider is backed by epoll (sun.nio.ch.EPollSelectorImpl),
// so calling Selector.select() is, a few layers down, calling epoll_wait()
// exactly like the C++ version of this experiment does explicitly by hand.
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.SocketChannel;
import java.util.Iterator;
import java.util.Set;

public class IoDemo {
    static final int RESPONSE_DELAY_MS = 100;
    static final int N = 20;

    static int startServer() throws IOException {
        ServerSocket server = new ServerSocket(0, 128, java.net.InetAddress.getByName("127.0.0.1"));
        int port = server.getLocalPort();
        Thread acceptor = new Thread(() -> {
            try {
                while (true) {
                    Socket client = server.accept();
                    Thread handler = new Thread(() -> {
                        try (Socket c = client) {
                            c.getInputStream().read(new byte[1024]);
                            Thread.sleep(RESPONSE_DELAY_MS);
                            c.getOutputStream().write("ok".getBytes());
                        } catch (Exception ignored) {
                        }
                    });
                    handler.setDaemon(true);
                    handler.start();
                }
            } catch (IOException ignored) {
            }
        });
        acceptor.setDaemon(true);
        acceptor.start();
        return port;
    }

    static void blockingRequest(int port) throws IOException {
        try (Socket s = new Socket("127.0.0.1", port)) {
            s.getOutputStream().write("ping".getBytes());
            s.getInputStream().read(new byte[1024]);
        }
    }

    static double benchSerial(int port) throws IOException {
        long start = System.nanoTime();
        for (int i = 0; i < N; i++) blockingRequest(port);
        return (System.nanoTime() - start) / 1e9;
    }

    // The NIO version: N non-blocking channels, one Selector (epoll under
    // the hood on Linux), a loop that asks the kernel which channels are
    // ready instead of parking a thread per connection.
    static double benchSelector(int port) throws IOException {
        long start = System.nanoTime();
        Selector selector = Selector.open();
        int remaining = N;

        for (int i = 0; i < N; i++) {
            SocketChannel ch = SocketChannel.open();
            ch.configureBlocking(false); // the key line: this channel will never block a thread
            ch.connect(new InetSocketAddress("127.0.0.1", port));
            ch.register(selector, SelectionKey.OP_CONNECT);
        }

        while (remaining > 0) {
            selector.select(); // blocks ONLY here, waiting on ALL channels at once -- this is epoll_wait
            Set<SelectionKey> keys = selector.selectedKeys();
            Iterator<SelectionKey> it = keys.iterator();
            while (it.hasNext()) {
                SelectionKey key = it.next();
                it.remove();
                SocketChannel ch = (SocketChannel) key.channel();
                if (key.isConnectable()) {
                    ch.finishConnect();
                    ch.write(ByteBuffer.wrap("ping".getBytes()));
                    ch.register(selector, SelectionKey.OP_READ);
                } else if (key.isReadable()) {
                    ByteBuffer buf = ByteBuffer.allocate(1024);
                    ch.read(buf);
                    key.cancel();
                    ch.close();
                    remaining--;
                }
            }
        }
        selector.close();
        return (System.nanoTime() - start) / 1e9;
    }

    public static void main(String[] args) throws IOException {
        int port = startServer();
        double tSerial = benchSerial(port);
        double tSelector = benchSelector(port);
        System.out.printf("N=%d requests, %dms server delay each%n", N, RESPONSE_DELAY_MS);
        System.out.printf("serial (java.net.Socket, blocking): %.3fs  (~%.0fms/req)%n", tSerial, tSerial / N * 1000.0);
        System.out.printf("concurrent (java.nio Selector):     %.3fs%n", tSelector);
    }
}
