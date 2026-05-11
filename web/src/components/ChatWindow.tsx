import type { ChatSocket } from "../hooks/useChatSocket";
import { InputBar } from "./InputBar";
import { MessageList } from "./MessageList";
import { TopBar } from "./TopBar";

interface Props {
  socket: ChatSocket;
}

export function ChatWindow({ socket }: Props) {
  return (
    <main className="flex h-full flex-1 flex-col">
      <TopBar socket={socket} />
      <MessageList />
      <InputBar socket={socket} />
    </main>
  );
}
