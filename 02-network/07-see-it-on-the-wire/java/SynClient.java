// Layer 2 · Topic 7 - Java's contribution to the SYN table.
//
// One HttpClient instance, reused. Its pool is not exposed through the public
// API, so the only way to find out whether it pooled is to count from the
// other end -- which is exactly what this topic is about.
//
//   LAB_URL=http://127.0.0.1:8000/work java -cp /tmp/javabuild SynClient
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

public class SynClient {
    public static void main(String[] args) throws Exception {
        String url = System.getenv().getOrDefault("LAB_URL", "http://127.0.0.1:8000/work");
        int n = Integer.parseInt(System.getenv().getOrDefault("LAB_REQUESTS", "30"));

        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .version(HttpClient.Version.HTTP_1_1)   // match the other five
                .build();
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).build();

        long t0 = System.nanoTime();
        for (int i = 0; i < n; i++) {
            client.send(req, HttpResponse.BodyHandlers.ofString());
        }
        System.out.printf("one HttpClient instance, %d requests in %d ms%n",
                n, (System.nanoTime() - t0) / 1_000_000L);
    }
}
