from .base import LLMProvider
class EchoProvider(LLMProvider):
    def complete(self, messages: list[dict], temperature: float = 0.4) -> str:
        user_message = messages[-1]["content"] if messages else ""
        if "restaurant" in user_message.lower() or "resto" in user_message.lower():
            return "Je peux viser quelque chose de léger mais plaisant. Il me manque surtout le quartier, l'heure et le budget approximatif. Je te proposerai ensuite quelques options avant toute réservation."
        return "Réponse de test du Daemon Harness. Configure LLM_PROVIDER=openai ou local pour utiliser un vrai modèle."
