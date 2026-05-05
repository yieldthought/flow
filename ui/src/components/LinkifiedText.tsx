import { openExternalUrl } from "../tauri";

const URL_RE = /https?:\/\/[^\s<>"']+/gi;
const TRAILING_PUNCTUATION_RE = /[),.;:!?]+$/;

export function LinkifiedText({ text }: { text: string }) {
  const parts = linkParts(text);
  return (
    <span className="linkified-text">
      {parts.map((part, index) => {
        const href = part.href;
        return href ? (
          <a
            className="external-link"
            href={href}
            key={`${href}-${index}`}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              void openExternalUrl(href);
            }}
          >
            {part.text}
          </a>
        ) : (
          <span key={`${part.text}-${index}`}>{part.text}</span>
        );
      })}
    </span>
  );
}

export function linkParts(text: string): Array<{ text: string; href?: string }> {
  const parts: Array<{ text: string; href?: string }> = [];
  let lastIndex = 0;
  for (const match of text.matchAll(URL_RE)) {
    const raw = match[0];
    const index = match.index ?? 0;
    if (index > lastIndex) {
      parts.push({ text: text.slice(lastIndex, index) });
    }
    const trailing = raw.match(TRAILING_PUNCTUATION_RE)?.[0] ?? "";
    const href = raw.slice(0, raw.length - trailing.length);
    if (href) {
      parts.push({ text: href, href });
    }
    if (trailing) {
      parts.push({ text: trailing });
    }
    lastIndex = index + raw.length;
  }
  if (lastIndex < text.length) {
    parts.push({ text: text.slice(lastIndex) });
  }
  return parts.length ? parts : [{ text }];
}
