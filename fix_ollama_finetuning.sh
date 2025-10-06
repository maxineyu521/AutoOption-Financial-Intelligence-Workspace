#!/bin/bash
# Fix Ollama fine-tuning setup

echo "🔧 Fixing Ollama fine-tuning"
echo "========================"

echo "🔍 Step 1: Check if Modelfile exists..."
if ! docker exec financial_agent_app ls -la /app/src/tools/modelfile > /dev/null 2>&1; then
    echo "❌ Modelfile not found, creating..."
    docker exec financial_agent_app python src/tools/setup_ollama_finetuning.py
fi

echo ""
echo "🔍 Step 2: Copy Modelfile into Ollama container..."
docker cp financial_agent_app:/app/src/tools/modelfile ./modelfile
docker cp ./modelfile ollama_llm:/modelfile
echo "✅ Modelfile copied"

echo ""
echo "🔍 Step 3: Create model inside Ollama container..."
if docker exec ollama_llm ollama create options-expert -f /modelfile; then
    echo "✅ Model created successfully"
else
    echo "❌ Model creation failed"
    exit 1
fi

echo ""
echo "🔍 Step 4: Verify model..."
docker exec ollama_llm ollama list

echo ""
echo "🔍 Step 5: Test model..."
echo "Testing model, please wait..."
docker exec ollama_llm ollama run options-expert "Analyze options trading opportunities for AAPL"

echo ""
echo "✅ Ollama fine-tuning setup complete!"
echo "You can now update generate_production_report.py to use the new model"
