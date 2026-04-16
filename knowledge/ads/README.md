# Ads Knowledge Files

Wrzucaj tutaj pliki `.md` z założeniami reklamowymi, notatkami strategicznymi, testami, insightami i regułami operacyjnymi.

Ścieżka:
`knowledge/ads/*.md`

Jak to działa:
- Pipeline `scripts/fetch_data.py` przy każdym odświeżeniu dashboardu czyta wszystkie markdowny z tego folderu.
- Zawartość jest dodawana do `data/senns_data.json` jako `knowledge_context`.
- Claude Chat w dashboardzie automatycznie dołącza ten kontekst do promptu przed odpowiedzią.

Wskazówki:
- Najlepiej 1 temat = 1 plik.
- Używaj konkretnych tytułów, np. `offer-positioning.md`, `meta-scaling-rules.md`, `icp-objections.md`.
- Gdy plik jest bardzo długi, kontekst może zostać automatycznie skrócony do bezpiecznego limitu.
