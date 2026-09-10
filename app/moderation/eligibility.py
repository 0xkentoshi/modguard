from app.agent.schemas import MessageContext


def should_analyze_with_llm(
    context: MessageContext,
) -> bool:
    """
    Text/captions are eligible for the current text moderation pipeline.

    Empty service events are stored by the surrounding system but must not
    waste an LLM request or create false-positive moderation.
    """

    return bool(
        context.current_message.raw_text.strip()
    )
