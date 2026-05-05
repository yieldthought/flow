import { eventCardTitle } from "../format";
import type { FocusEvent } from "../types";
import { LinkifiedText } from "./LinkifiedText";

interface Props {
  events: FocusEvent[];
  hoveredKey: string | null;
  pinnedKey: string | null;
  onHoverKey: (key: string | null) => void;
  onPinKey: (key: string | null) => void;
}

export function EventStrip({ events, hoveredKey, pinnedKey, onHoverKey, onPinKey }: Props) {
  function toggleEvent(key: string | null) {
    if (key) {
      onPinKey(key);
    }
  }

  return (
    <section className="event-strip">
      <div className="event-strip__label">history</div>
      <div className="event-strip__scroller">
        {events.map((event) => {
          const active = !!event.link && (event.link.key === hoveredKey || event.link.key === pinnedKey);
          const linkKey = event.link?.key ?? null;
          return (
            <article
              key={event.id}
              className={["event-card", active ? "event-card--active" : ""].join(" ")}
              role="button"
              tabIndex={0}
              onMouseEnter={() => onHoverKey(linkKey)}
              onMouseLeave={() => onHoverKey(null)}
              onClick={() => toggleEvent(linkKey)}
              onKeyDown={(keyboardEvent) => {
                if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") {
                  keyboardEvent.preventDefault();
                  toggleEvent(linkKey);
                }
              }}
            >
              <div className="event-card__time">{eventCardTitle(event)}</div>
              <div className="event-card__text">
                <LinkifiedText text={event.text} />
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
