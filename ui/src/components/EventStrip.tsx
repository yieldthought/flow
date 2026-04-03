import { eventCardTitle } from "../format";
import type { FocusEvent } from "../types";

interface Props {
  events: FocusEvent[];
  hoveredKey: string | null;
  pinnedKey: string | null;
  onHoverKey: (key: string | null) => void;
  onPinKey: (key: string | null) => void;
}

export function EventStrip({ events, hoveredKey, pinnedKey, onHoverKey, onPinKey }: Props) {
  return (
    <section className="event-strip">
      <div className="event-strip__label">history</div>
      <div className="event-strip__scroller">
        {events.map((event) => {
          const active = !!event.link && (event.link.key === hoveredKey || event.link.key === pinnedKey);
          return (
            <button
              key={event.id}
              type="button"
              className={["event-card", active ? "event-card--active" : ""].join(" ")}
              onMouseEnter={() => onHoverKey(event.link?.key ?? null)}
              onMouseLeave={() => onHoverKey(null)}
              onClick={() => onPinKey(event.link?.key ?? null)}
            >
              <div className="event-card__time">{eventCardTitle(event)}</div>
              <div className="event-card__text">{event.text}</div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
