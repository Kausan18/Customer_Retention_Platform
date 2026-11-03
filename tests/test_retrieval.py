from rag.retriever import search_policy

result = search_policy("what is the refund window?")
print("success:", result["success"])
for p in result["passages"]:
    print(f"{p['similarity_score']:.4f} | {p['document']} | {p['section']}")
print("\nTop passage:\n", result["passages"][0]["content"][:300])