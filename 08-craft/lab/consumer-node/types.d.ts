// Types generated from the COMMITTED openapi.snapshot.json.
//
// WHAT THIS DEMONSTRATES: the contrast with consumer-go. The Go build FAILS on
// an int->string field change. TypeScript also fails -- at TYPECHECK. Neither
// exists at runtime, and that difference is why both consumers are in this lab.
//
// WHAT TO LOOK FOR: nothing in this file runs. A response that violates every
// line of it will be `JSON.parse`d into a variable typed by it, and TypeScript
// will believe you. `client.js` shows both halves: the typed read that trusts,
// and the runtime validator that does not.
//
// Regenerate with:  npx openapi-typescript ../api/openapi.snapshot.json -o types.d.ts

export interface CustomerOrderOut {
  id: number;
  status: string;
  total_cents: number;
}

export interface CustomerOrderListOut {
  items: CustomerOrderOut[];
  /** Topic 6 break 1 changes this to `string` in the provider. */
  total: number;
}

export interface ApiError {
  error: string;
  message: string;
}
