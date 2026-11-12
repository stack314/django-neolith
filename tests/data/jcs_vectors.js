// Vectors for RFC 8785: doubles by their IEEE 754 bits, and values with the
// canonical text JSON.stringify gives once members are sorted by UTF-16 units.
const crypto = require("crypto");
function hexToDouble(h) { const b = Buffer.from(h, "hex"); return b.readDoubleBE(0); }
const edge = [
  "0000000000000000", "8000000000000000", "0000000000000001", "8000000000000001",
  "7fefffffffffffff", "ffefffffffffffff", "4340000000000000", "c340000000000000",
  "4430000000000000", "44b52d02c7e14af5", "44b52d02c7e14af6", "44b52d02c7e14af7",
  "444b1ae4d6e2ef4e", "444b1ae4d6e2ef4f", "444b1ae4d6e2ef50", "3eb0c6f7a0b5ed8c",
  "3eb0c6f7a0b5ed8d", "3eb0c6f7a0b5ed8e", "41b3de4355555553", "41b3de4355555554",
  "41b3de4355555555", "41b3de4355555556", "41b3de4355555557", "becbf647612f3696",
  "43143ff3c1cb0959", "3ff0000000000000", "3fb999999999999a", "3fd3333333333334",
  "4059000000000000", "433fffffffffffff", "3f1a36e2eb1c432d", "3eb0c6f7a0b5ed8d",
  "3e7ad7f29abcaf48", "0010000000000000", "000fffffffffffff", "4202a05f20000000",
];
// The sample values of RFC 8785 Appendix B are among them (for example
// 44b52d02c7e14af6 is 1e+23 and 3eb0c6f7a0b5ed8d is 0.000001).
const numbers = [];
const seen = new Set();
function add(h) {
  if (seen.has(h)) return;
  const d = hexToDouble(h);
  if (!Number.isFinite(d)) return;
  seen.add(h);
  numbers.push([h, JSON.stringify(d)]);
}
edge.forEach(add);
// A fixed pseudo-random walk over the bit patterns, so the file is reproducible.
let state = crypto.createHash("sha256").update("neolith jcs vectors").digest();
while (numbers.length < 400) {
  state = crypto.createHash("sha256").update(state).digest();
  add(state.subarray(0, 8).toString("hex"));
}
// Every decimal exponent, to cross each of toString's branches.
for (let e = -330; e <= 310; e += 7) {
  for (const m of ["1", "1.5", "9.999999999999999"]) {
    const d = Number(m + "e" + e);
    if (!Number.isFinite(d)) continue;
    const b = Buffer.alloc(8); b.writeDoubleBE(d, 0); add(b.toString("hex"));
  }
}
// RFC 8785 section 3.2.3: members sorted by the UTF-16 code units of their
// names. Written out member by member, since a JavaScript object lists
// integer-like names first whatever order they were added in.
function canonical(v) {
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  if (v && typeof v === "object") {
    // Array.prototype.sort on strings compares UTF-16 code units.
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
  }
  return JSON.stringify(v);
}
const values = [
  {},
  [],
  { numbers: [333333333.3333333, 1e30, 4.5, 0.002, 1e-27], string: "€$\u000f\nA'B\"\\\\\"/", literals: [null, true, false] },
  { "€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With Dagesh", "1": "One", "😀": "Emoji: Grinning Face", "\u0080": "Control", "ö": "Latin Small Letter O With Diaeresis" },
  { b: [{ z: 1, a: [] }, [[]], ""], a: { "": 0, " ": -0 }, "A": "\u0000\u001f\u007f  ￿" },
  ["k", "inventory.v1", "reserve", "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU", 0],
  { claims: { roles: ["clerk"] }, issuer: "https://id.example.com", subject: "u-81f3" },
  { "1": 1, "10": 2, "2": 3, "a\u0000": 4, "a": 5, "𐀀": 6, "￿": 7 },
];
const objects = values.map((v) => [JSON.stringify(v), canonical(v)]);
process.stdout.write(JSON.stringify({
  _source: "Node.js " + process.versions.node + ": JSON.stringify of doubles given by their IEEE 754 bits (ECMAScript Number::toString), and of values written member by member with names sorted by UTF-16 code units, as RFC 8785 specifies. The first member of each pair under 'values' is the input as JSON text, the second its canonical text.",
  numbers, values: objects }, null, 0).replace(/\],\[/g, "],\n[") + "\n");
