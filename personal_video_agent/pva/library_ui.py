"""Streamlit controls for browsing, organizing and exporting saved summaries."""
from contextlib import closing
from datetime import date
from urllib.parse import quote, urlencode, urlsplit

import streamlit as st

from . import history, library, presentation, store, summary_export

PAGE_SIZE = 10
FILTER_WIDGETS = {
    "collections": "library_collections_filter",
    "topics": "library_topics_filter",
    "sources": "library_sources_filter",
    "field": "library_date_field",
    "dates": "library_date_range",
    "order": "library_sort",
}


def metadata(entry, collection_names=()):
    parts = [library.source_label(entry), presentation.category_label(entry.get("category"))]
    if entry.get("author"):
        parts.append(entry["author"])
    if entry.get("published_at"):
        parts.append(entry["published_at"])
    elif day := library.source_date(entry.get("created_at")):
        parts.append(f"Added {day}")
    return " · ".join([*parts, *collection_names])


@st.dialog("Collections")
def edit_collections(db_path, entry):
    with closing(store.connect(db_path)) as db:
        _collection_form(db, entry)


def _collection_form(db, entry):
    names, memberships = library.collection_state(db)
    selected = memberships.get(entry["id"], set())
    suggested = [item for item in library.suggested_collections(db, entry) if item not in selected]
    ordered = list(dict.fromkeys([*suggested, *names]))
    st.caption(entry["title"])
    if suggested:
        st.caption("Suggested from your saves · " + ", ".join(names[key] for key in suggested[:3]))
    with st.form("assign_collections"):
        ids = st.multiselect("Add to collections", ordered, default=[key for key in ordered if key in selected],
                             format_func=names.__getitem__)
        new_name = st.text_input("Create and add to a new collection", max_chars=60)
        if st.form_submit_button("Save collections", type="primary"):
            try:
                library.save_collections(db, entry["id"], ids, new_name)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.rerun()


@st.dialog("Export or share summary", width="large")
def export_summary(db_path, entry):
    with closing(store.connect(db_path)) as db:
        _export_form(db, entry)


def _export_form(db, entry):
    video = {**store.detail(db, entry["id"]), "source_kind": entry["source_kind"]}
    st.caption(video["title"])
    available = ["Source details", "Supporting evidence", "Full source text"]
    if any(row["translated_text"] for row in video["segments"]):
        available.append("Translated transcript")
    if any(row["extracted_text"] for row in video["frames"]):
        available.append("Visual text")
    with st.form("summary_export"):
        format_name = st.selectbox("Format", list(summary_export.FORMATS))
        options = st.multiselect("Also include", available, default=["Source details", "Supporting evidence"])
        prepared = st.form_submit_button("Prepare export", type="primary")
    if prepared:
        names, memberships = library.collection_state(db)
        folders = [names[key] for key in names if key in memberships.get(video["id"], ())]
        sections = summary_export.build_sections(video, options, folders)
        ext, mime = summary_export.FORMATS[format_name]
        name = summary_export.filename(video, ext)
        try:
            data = summary_export.render(video["title"], sections, format_name)
            body = summary_export.plain_text(video["title"], [section for section in sections
                                                              if section[0] in {"Source details", "Summary"}])
            draft = summary_export.email_draft(video["title"], body, data, name, mime)
        except (ValueError, RuntimeError, ImportError) as exc:
            st.error(f"Could not prepare this export: {exc}")
            return
        download, mail = st.columns(2)
        download.download_button(f"Download {format_name}", data, file_name=name, mime=mime,
                                 on_click="ignore", width="stretch")
        mail.download_button("Download email draft", draft, file_name=summary_export.filename(video, "eml"),
                             mime="message/rfc822", on_click="ignore", width="stretch",
                             help="Open the draft in a compatible mail app, add recipients and send.")
        with st.expander("Copy text to share"):
            st.code(body, language=None, wrap_lines=True)
            if len(body) <= 1500:
                query = urlencode({"subject": " ".join(video["title"].split()), "body": body},
                                  quote_via=quote)
                st.link_button("Open email app", "mailto:?" + query,
                               help="Opens a draft with this text. Add the recipient and send in your mail app.")


def clear_filters(*fields):
    saved = dict(st.session_state.get("library_filter_values", {}))
    for field in fields or FILTER_WIDGETS:
        saved.pop(field, None)
        st.session_state.pop(FILTER_WIDGETS[field], None)
    st.session_state["library_filter_values"] = saved
    st.session_state["library_page"] = 1


def page_navigation(current, total, scope="library"):
    if total <= 1:
        return

    def go(number):
        st.session_state[f"{scope}_page"] = number

    with st.container(horizontal=True, horizontal_alignment="center", gap="small", key=f"{scope}_pages"):
        st.button("‹", key=f"{scope}_prev_page", disabled=current == 1,
                  help="Previous page", on_click=go, args=(current - 1,))
        previous = 0
        for number in sorted({1, total, *range(max(1, current - 2), min(total, current + 2) + 1)}):
            if number > previous + 1:
                st.caption("…")
            st.button(str(number), key=f"{scope}_page_{number}", disabled=current == number,
                      help=f"Page {number}", on_click=go, args=(number,))
            previous = number
        st.button("›", key=f"{scope}_next_page", disabled=current == total,
                  help="Next page", on_click=go, args=(current + 1,))


@st.dialog("Remove local copy")
def confirm_remove(manager, video_id, title, *, summary=True):
    st.write(title)
    st.caption("This removes the local summary, source copies, and evidence for this item. "
               "Processing history keeps the source link or filename, time and outcome.")
    if st.button("Delete summary" if summary else "Remove local data", type="primary",
                 key=f"confirm_remove_{video_id}"):
        try:
            failed = manager.remove_library_item(video_id)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
            return
        st.session_state.pop("selected", None)
        st.session_state["raccoon_message"] = ("Local copy removed. Source kept in History.", False)
        st.session_state["raccoon_cleanup_warnings"] = failed
        st.rerun()


def history_panel(db, manager):
    outcomes = {"": "All outcomes", "ready": "Succeeded", "failed": "Failed", "skipped": "Removed from queue"}
    selected = st.selectbox("Outcome", list(outcomes), format_func=outcomes.__getitem__,
                            key="history_outcome", on_change=lambda: st.session_state.pop("history_page", None))
    current = st.session_state.get("history_page", 1)
    rows, count = history.page(db, selected, (current - 1) * PAGE_SIZE, PAGE_SIZE)
    total_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    if current > total_pages:
        current = total_pages
        rows, _ = history.page(db, selected, (current - 1) * PAGE_SIZE, PAGE_SIZE)
    st.session_state["history_page"] = current
    if not rows:
        st.info("No processing history for this outcome.")
        return
    st.caption(f"{count} result(s) · Page {current} of {total_pages}")
    for row in rows:
        with st.container(border=True, key=f"history_row_{row['id']}"):
            st.subheader(row["title"] or row["source"], anchor=False)
            state = ("Local data removed" if row["item_status"] is None else
                     "In Library" if row["item_status"] == "ready" else "Local item retained")
            st.caption(f"{outcomes[row['outcome']]} · {state} · {row['at'].replace('T', ' ')}")
            with st.container(horizontal=True):
                if row["source_kind"] in {"video", "webpage"} and urlsplit(row["source"]).scheme in {"https", "http"}:
                    st.link_button("Open source", row["source"])
                if row["item_status"] == "ready" and st.button("Read summary", key=f"history_open_{row['id']}"):
                    st.session_state["selected"] = row["video_id"]
                    st.session_state["raccoon_next_page"] = "Library"
                    st.rerun()
                if row["item_status"] in {"failed", "cancelled", "new"} and st.button(
                    "Remove local data", key=f"history_remove_{row['id']}"
                ):
                    confirm_remove(manager, row["video_id"], row["title"] or row["source"], summary=False)
    page_navigation(current, total_pages, "history")


def listing(db, db_path, rows, manager):
    names, memberships = library.collection_state(db)
    topics = sorted({item["category"] or "" for item in rows}, key=presentation.category_label)
    saved = st.session_state.get("library_filter_values", {})
    left, right = st.columns([1, 2.65], gap="large")
    with left, st.container(border=True, key="library_filters_panel"):
        st.subheader("Filters", anchor=False)
        st.button("Clear all filters", key="library_clear_all", on_click=clear_filters)
        collections = st.multiselect("Collections", list(names),
            default=[key for key in saved.get("collections", []) if key in names],
            format_func=names.__getitem__, key="library_collections_filter")
        if collections:
            st.button("Clear collections", on_click=clear_filters, args=("collections",))
        topic = st.multiselect("Topics", topics, key="library_topics_filter",
            default=[value for value in saved.get("topics", []) if value in topics],
            format_func=presentation.category_label)
        if topic:
            st.button("Clear topics", on_click=clear_filters, args=("topics",))
        sources = st.multiselect("Source type", list(library.SOURCES), format_func=library.SOURCES.__getitem__,
                                 default=saved.get("sources", []), key="library_sources_filter")
        if sources:
            st.button("Clear source type", on_click=clear_filters, args=("sources",))
        fields = {"created_at": "Date added", "published_at": "Publication date"}
        field = st.selectbox("Date field", list(fields), format_func=fields.__getitem__,
                             index=int(saved.get("field") == "published_at"), key="library_date_field")
        if field != "created_at":
            st.button("Reset date field", on_click=clear_filters, args=("field",))
        dates = st.date_input("Date range", value=saved.get("dates", ()), min_value=date(1900, 1, 1),
                              max_value=date(2100, 12, 31), key="library_date_range",
                              help="Pick a start and end date in the calendar. Clear the field to show all dates.")
        if dates:
            st.button("Clear date range", key="library_clear_dates", on_click=clear_filters, args=("dates",))
        orders = ["Latest first", "Oldest first", "Title A–Z"]
        order = st.selectbox("Sort", orders, index=orders.index(saved.get("order", orders[0])),
                             key="library_sort")
        if order != orders[0]:
            st.button("Reset sort", on_click=clear_filters, args=("order",))
        current_filters = dict(collections=collections, topics=topic, sources=sources,
                               dates=dates, field=field, order=order)
        if current_filters != saved:
            st.session_state["library_page"] = 1
        st.session_state["library_filter_values"] = current_filters
        with st.expander("New collection"):
            with st.form("new_library_collection", clear_on_submit=True):
                name = st.text_input("Collection name", max_chars=60)
                if st.form_submit_button("Create collection"):
                    try:
                        library.create_collection(db, name)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
    result = library.filtered(rows, memberships, collections=set(collections), topics=set(topic),
                              sources=set(sources), dates=dates or (), field=field, order=order)
    total_pages = max(1, (len(result) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = min(st.session_state.get("library_page", 1), total_pages)
    st.session_state["library_page"] = current_page
    start = (current_page - 1) * PAGE_SIZE
    with right, st.container(key="library_list"):
        if result:
            st.caption(f"{start + 1}–{min(start + PAGE_SIZE, len(result))} of {len(result)} items"
                       + (f" · Page {current_page} of {total_pages}" if total_pages > 1 else ""))
        if not result:
            st.info("No items match your filters." if rows else "Add a source to start your library.")
        for entry in result[start:start + PAGE_SIZE]:
            video_id = entry["id"]
            ready = entry["status"] == "ready"
            folders = [names[key] for key in names if key in memberships.get(video_id, ())]
            with st.container(border=True, key=f"library_row_{video_id}"):
                st.subheader(entry["title"], anchor=False)
                st.caption(metadata(entry, folders) + ("" if ready else " · " + entry["status"].title()))
                with st.container(horizontal=True, gap="small"):
                    if st.button("Read summary" if ready else "View item", key=f"library_open_{video_id}"):
                        st.session_state["selected"] = video_id
                        st.rerun()
                    if ready and st.button("Export / share", key=f"library_export_{video_id}"):
                        export_summary(db_path, entry)
                    if ready and st.button("Collections", key=f"library_collect_{video_id}"):
                        edit_collections(db_path, entry)
                    if ready and st.button("Delete summary", key=f"library_delete_{video_id}"):
                        confirm_remove(manager, video_id, entry["title"])
        page_navigation(current_page, total_pages)
