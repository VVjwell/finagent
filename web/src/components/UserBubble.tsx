interface Props {
  text: string;
}

export function UserBubble({ text }: Props) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-2xl rounded-tr-md bg-sky-500/90 px-4 py-2 text-sm leading-relaxed text-white whitespace-pre-wrap">
        {text}
      </div>
    </div>
  );
}
