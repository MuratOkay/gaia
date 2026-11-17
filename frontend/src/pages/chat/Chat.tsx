import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  api,
  type AnswerDoc,
  type ChatMessage,
  type ChatSource,
  type ConversationView,
  type Me,
  stream,
  type StreamEvent,
} from "../../api/client";
import { AssistantTurn, ResultTable, Stage } from "../../components/chat";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

const FINAL = new Set(["answer", "clarification", "cannot_answer", "failed", "refused"]);
const KIND: Record<string, ChatMessage["kind"]> = {
  answer: "answer", clarification: "clarification", cannot_answer: "cannot_answer", failed: "failed", refused: "refused",
};

interface Pending {
  stage: string;
  result?: { columns: string[]; rows: unknown[][]; personal_columns?: number[]; masked?: boolean };
}

/** The turn's events applied to the conversation on screen: the user's message as soon as
 *  it is stored, the stage, the table as soon as it exists, then Gaia's turn. */
export function applyEvent(messages: ChatMessage[], pending: Pending | null, e: StreamEvent
): { messages: ChatMessage[]; pending: Pending | null } {
  if (e.event === "message") return { messages: [...messages, e.data as unknown as ChatMessage], pending: { stage: "retrieval" } };
  if (e.event === "status") return { messages, pending: { ...(pending ?? { stage: "" }), stage: String(e.data.stage) } };
  if (e.event === "result") {
    return { messages, pending: { ...(pending ?? { stage: "narrating" }), result: e.data as unknown as Pending["result"] } };
  }
  if (FINAL.has(e.event)) {
    const doc = e.data as unknown as AnswerDoc;
    const turn: ChatMessage = {
      id: String(doc.message_id ?? `live-${messages.length}`), seq: Number(doc.seq ?? messages.length + 1), role: "assistant",
      kind: KIND[e.event], content: doc.narrative ?? doc.reason ?? "", payload: doc, created_at: new Date().toISOString(),
      clarification_id: doc.clarification?.id ?? null,
    };
    return { messages: [...messages, turn], pending: null };
  }
  return { messages, pending };
}

export function ChatPage({ me }: { me: Me }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams();
  const sources = useLoad(() => api<ChatSource[]>("/api/data-sources"));
  const [ds, setDs] = useState("");
  const [tick, setTick] = useState(0);
  const conversations = useLoad(
    () => (ds ? api<ConversationView[]>("/api/conversations", { query: { data_source: ds } }) : Promise.resolve([])),
    [ds, tick],
  );
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const [newQuestion, setNewQuestion] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const abort = useRef<AbortController | null>(null);
  const started = useRef<string | null>(null); // a conversation this page just created
  const bottom = useRef<HTMLDivElement | null>(null);
  const canSeeSql = me.roles.includes("admin") || me.roles.includes("sme") || me.roles.includes("auditor");

  useEffect(() => {
    if (!ds && sources.data?.length) setDs((sources.data.find((s) => s.default) ?? sources.data[0]).name);
  }, [sources.data, ds]);

  const load = useCallback(async (cid: string) => {
    setError(null);
    try {
      const c = await api<ConversationView & { messages: ChatMessage[] }>(`/api/conversations/${cid}`);
      setMessages(c.messages);
      if (c.data_source !== ds) setDs(c.data_source);
    } catch (e) {
      setError(e as Error);
    }
  }, [ds]);

  useEffect(() => {
    if (id && id === started.current) {
      started.current = null; // its first turn is streaming: nothing to load
      return;
    }
    abort.current?.abort();
    setPending(null);
    if (id) void load(id);
    else setMessages([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  useEffect(() => () => abort.current?.abort(), []);
  // a block body: newer browsers return a Promise from scrollIntoView, and an effect that
  // returns anything but a function breaks React ("destroy is not a function", D78)
  useEffect(() => {
    bottom.current?.scrollIntoView?.({ block: "end" });
  }, [messages, pending]);

  const last = messages[messages.length - 1];
  const clarifying = last?.role === "assistant" && last.kind === "clarification";

  async function send(content: string, replyToClarification?: boolean) {
    if (!content.trim() || pending) return;
    setError(null);
    let cid = id;
    try {
      if (!cid) {
        const c = await api<ConversationView>("/api/conversations", { method: "POST", body: { data_source: ds } });
        cid = c.id;
        started.current = cid;
        setMessages([]);
        navigate(`/chat/${cid}`, { replace: false });
      }
      setText("");
      setPending({ stage: "retrieval" });
      abort.current = new AbortController();
      let state = { messages: id ? messages : [] as ChatMessage[], pending: { stage: "retrieval" } as Pending | null };
      await stream(`/api/conversations/${cid}/messages`, {
        content, ...(replyToClarification === false ? { reply_to_clarification: false } : {}),
      }, (e) => {
        state = applyEvent(state.messages, state.pending, e);
        setMessages(state.messages);
        setPending(state.pending);
      }, abort.current.signal);
      setTick((n) => n + 1);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(e as Error);
    } finally {
      setPending(null);
      setNewQuestion(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(text, clarifying && newQuestion ? false : undefined);
  }

  if (me.local || (sources.data && sources.data.length === 0)) {
    // Nothing to ask about — say why, and what would change it (D76: for the only account
    // the owner could use, this page looked empty)
    return (
      <section>
        <h1>{t("chat.title")}</h1>
        {me.local
          ? <p className="note">{t("chat.localAccount")}</p>
          : me.roles.includes("admin")
            ? <p className="note">{t("chat.nothingToReadAdmin")} <Link to="/admin/grants">{t("chat.toGrants")}</Link></p>
            : <p className="note">{t("chat.nothingToRead")}</p>}
      </section>
    );
  }

  return (
    <section className="chat">
      <aside className="conversations" aria-label={t("chat.conversations")}>
        <label>
          {t("chat.dataSource")}
          <select value={ds} onChange={(e) => { setDs(e.target.value); navigate("/chat"); }}>
            {sources.data?.map((s) => <option key={s.name} value={s.name} disabled={!s.usable}>{s.name}</option>)}
          </select>
        </label>
        <button type="button" onClick={() => navigate("/chat")}>{t("chat.new")}</button>
        <Busy loading={conversations.loading && !conversations.data} />
        <ul>
          {conversations.data?.map((c) => (
            <li key={c.id} className={c.id === id ? "current" : undefined}>
              <Link to={`/chat/${c.id}`}>{c.title ?? t("chat.untitled")}</Link>
              <div className="muted small">{when(c.updated_at, i18n.language)}</div>
            </li>
          ))}
        </ul>
      </aside>
      <div className="conversation">
        <ErrorNote error={sources.error ?? error} />
        {!id && messages.length === 0 && <p className="muted">{t("chat.intro", { ds })}</p>}
        <div className="turns" aria-live="polite">
          {messages.map((m) => m.role === "user"
            ? <div key={m.id} className="turn user"><p>{m.content}</p></div>
            : <AssistantTurn key={m.id} kind={m.kind} doc={m.payload} canSeeSql={canSeeSql}
                             open={m === last && clarifying && !pending}
                             onOption={(n) => void send(String(n))} />)}
          {pending && (
            <div className="turn assistant pending">
              {pending.result && <ResultTable columns={pending.result.columns} rows={pending.result.rows}
                                              masked={pending.result.masked ? pending.result.personal_columns ?? [] : []} />}
              <Stage stage={pending.stage} />
            </div>
          )}
          <div ref={bottom} />
        </div>
        <form className="composer" onSubmit={submit} aria-label={t("chat.ask")}>
          <textarea rows={2} value={text} placeholder={clarifying && !newQuestion ? t("chat.answerPlaceholder") : t("chat.placeholder")}
                    aria-label={t("chat.question")} onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(text, clarifying && newQuestion ? false : undefined); } }} />
          <div className="actions">
            <button type="submit" disabled={!!pending || !text.trim() || !ds}>{t("chat.send")}</button>
            {clarifying && (
              <label className="check small">
                <input type="checkbox" checked={newQuestion} onChange={(e) => setNewQuestion(e.target.checked)} />
                {t("chat.askNew")}
              </label>
            )}
          </div>
        </form>
      </div>
    </section>
  );
}
