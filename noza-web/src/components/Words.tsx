/** A headline that arrives one word at a time. Words wrapped in *stars* are set in italic.
 *
 * Each word is its own box so it can rise on its own, and a browser's page translation takes each box for a
 * sentence: "Nous lisons la terre" came out as "We let's read there earth". So the words on show are marked as not
 * to be translated, and the whole sentence is kept beside them as one piece of text: hidden from the eye, read by
 * a screen reader, and translated as a sentence. When Chrome translates the page it marks the document
 * (translated-ltr / translated-rtl), and the stylesheet then shows that sentence in place of the words. In a
 * browser that does not mark the page, the headline stays in the language it was written in. */
export function Words({ text, start = 0, step = 70 }: { text: string; start?: number; step?: number }) {
  const words = text.split(" ");
  return (
    <>
      <span className="words-whole">{text.replace(/\*/g, "")}</span>
      <span className="words-shown" aria-hidden="true" translate="no">
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
      </span>
    </>
  );
}
