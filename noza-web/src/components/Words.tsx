/** A headline that arrives one word at a time. Words wrapped in *stars* are set in italic. */
export function Words({ text, start = 0, step = 70 }: { text: string; start?: number; step?: number }) {
  const words = text.split(" ");
  return (
    <>
      {words.map((w, k) => {
        const italic = w.startsWith("*");
        const clean = w.replace(/\*/g, "");
        return (
          <span key={k}>
            <span className={`word ${italic ? "italic" : ""}`} style={{ animationDelay: `${start + k * step}ms` }}>
              {clean}
            </span>
            {k < words.length - 1 ? " " : ""}
          </span>
        );
      })}
    </>
  );
}
