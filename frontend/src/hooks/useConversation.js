import { useState, useCallback, useRef, useEffect } from 'react';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';
import api from '../api';

/**
 * useConversation — Manages a Converse workflow conversation.
 *
 * Returns: { messages, conversationId, isWaitingForAI, latestLlmNodeId, sendMessage, reset }
 */
export function useConversation({ aiUsage = 'none' } = {}) {
  const [conversationId, setConversationId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [isWaitingForAI, setIsWaitingForAI] = useState(false);
  const [latestLlmNodeId, setLatestLlmNodeId] = useState(null);
  const [pendingLlmNodeId, setPendingLlmNodeId] = useState(null);
  const conversationIdRef = useRef(null);
  const latestLlmNodeIdRef = useRef(null);

  // Poll for LLM completion when waiting
  const { data: llmData, status: llmStatus } = useAsyncTaskPolling(
    pendingLlmNodeId ? `/nodes/${pendingLlmNodeId}/llm-status` : null,
    { enabled: !!pendingLlmNodeId, interval: 1500 }
  );

  // When LLM completes, update messages
  useEffect(() => {
    if (llmStatus === 'completed' && llmData?.content) {
      setMessages(prev => prev.map(m =>
        m.id === pendingLlmNodeId
          ? {
              ...m,
              content: llmData.content,
              llm_task_status: 'completed',
              tool_calls_meta: llmData.tool_calls_meta || null,
            }
          : m
      ));
      setLatestLlmNodeId(pendingLlmNodeId);
      latestLlmNodeIdRef.current = pendingLlmNodeId;
      setPendingLlmNodeId(null);
      setIsWaitingForAI(false);
    } else if (llmStatus === 'failed') {
      setMessages(prev => prev.map(m =>
        m.id === pendingLlmNodeId
          ? { ...m, content: 'Response failed. Please try again.', llm_task_status: 'failed' }
          : m
      ));
      setPendingLlmNodeId(null);
      setIsWaitingForAI(false);
    }
  }, [llmStatus, llmData, pendingLlmNodeId]);

  const sendMessage = useCallback(async (content) => {
    if (!content.trim()) return;

    // Add user message optimistically
    const tempUserMsg = {
      id: `temp-user-${Date.now()}`,
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, tempUserMsg]);
    setIsWaitingForAI(true);

    try {
      let res;
      if (!conversationIdRef.current) {
        // Start new conversation
        res = await api.post('/textmode/start', { content, ai_usage: aiUsage });
        const convId = res.data.conversation_id;
        setConversationId(convId);
        conversationIdRef.current = convId;
      } else {
        // Continue conversation — pass parent_id to target the correct branch
        const payload = { content };
        if (latestLlmNodeIdRef.current) {
          payload.parent_id = latestLlmNodeIdRef.current;
        }
        res = await api.post(`/textmode/${conversationIdRef.current}/message`, payload);
      }

      const { user_node_id, llm_node_id } = res.data;

      // Replace temp user message with real one
      setMessages(prev => {
        const updated = [...prev];
        const tempIdx = updated.findIndex(m => m.id === tempUserMsg.id);
        if (tempIdx >= 0) {
          updated[tempIdx] = { ...updated[tempIdx], id: user_node_id };
        }
        // Add placeholder AI message
        updated.push({
          id: llm_node_id,
          role: 'assistant',
          content: '',
          created_at: new Date().toISOString(),
          llm_task_status: 'pending',
        });
        return updated;
      });

      setPendingLlmNodeId(llm_node_id);

    } catch (err) {
      console.error('Send message error:', err);
      setMessages(prev => prev.filter(m => m.id !== tempUserMsg.id));
      setIsWaitingForAI(false);
    }
  }, [aiUsage]);

  const loadExistingThread = useCallback(async (nodeId) => {
    try {
      const res = await api.get(`/textmode/from-node/${nodeId}`);
      const convId = res.data.conversation_id;
      const msgs = res.data.messages || [];
      setConversationId(convId);
      conversationIdRef.current = convId;
      setMessages(msgs);
      // Find the latest completed LLM node
      const llmMsgs = msgs.filter(m => m.role === 'assistant' && m.llm_task_status === 'completed');
      if (llmMsgs.length > 0) {
        const lastId = llmMsgs[llmMsgs.length - 1].id;
        setLatestLlmNodeId(lastId);
        latestLlmNodeIdRef.current = lastId;
      }
    } catch (err) {
      console.error('Failed to load thread:', err);
    }
  }, []);

  const reset = useCallback(() => {
    setConversationId(null);
    conversationIdRef.current = null;
    setMessages([]);
    setIsWaitingForAI(false);
    setLatestLlmNodeId(null);
    latestLlmNodeIdRef.current = null;
    setPendingLlmNodeId(null);
  }, []);

  return {
    conversationId,
    messages,
    isWaitingForAI,
    latestLlmNodeId,
    sendMessage,
    loadExistingThread,
    reset,
  };
}
