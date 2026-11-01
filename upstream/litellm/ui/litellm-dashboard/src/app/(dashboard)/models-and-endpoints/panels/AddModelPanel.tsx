"use client";

import { useQueryStates } from "nuqs";
import { subscriptionPrefillParsers } from "@/components/subscriptions/modelPrefill";
import { useEffect, useMemo, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { useQueryClient } from "@tanstack/react-query";
import AddModelForm from "@/components/add_model/AddModelForm";
import { handleAddModelSubmit } from "@/components/add_model/handle_add_model_submit";
import {
  projectMountedValues,
  useMountRegistry,
  type MountedFormValues,
} from "@/components/common_components/MountedFormField";
import { Providers, getPlaceholder, getProviderModels } from "@/components/provider_info_helpers";
import { useModelCostMap } from "@/app/(dashboard)/hooks/models/useModelCostMap";
import { useCredentials } from "@/app/(dashboard)/hooks/credentials/useCredentials";
import { useTeams } from "@/app/(dashboard)/hooks/teams/useTeams";
import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { buildDecisionCatalog } from "@/lib/decisionModels";

const INITIAL_VALUES: MountedFormValues = { litellm_credential_name: null };

export default function AddModelPanel() {
  const { accessToken } = useAuthorized();
  const [params, setParams] = useQueryStates(subscriptionPrefillParsers);
  const [prefill] = useState(() => (params.subscription && params.subscription_model ? params : null));
  const form = useForm<MountedFormValues>({
    mode: "onChange",
    defaultValues: prefill
      ? {
          ...INITIAL_VALUES,
          custom_llm_provider: "CHATGPT",
          subscription_id: prefill.subscription,
          model: [`chatgpt/${prefill.subscription_model}`],
        }
      : INITIAL_VALUES,
  });
  useEffect(() => {
    if (prefill) void setParams({ subscription: null, subscription_model: null });
  }, [prefill, setParams]);
  const registry = useMountRegistry();
  const queryClient = useQueryClient();
  const { data: modelCostMapData } = useModelCostMap(true, true);
  const { data: credentialsResponse } = useCredentials();
  const { data: teams } = useTeams();
  const [selectedProvider, setSelectedProvider] = useState<string | null>(prefill ? "CHATGPT" : Providers.Anthropic);
  const pickedProvider = useWatch({ control: form.control, name: "custom_llm_provider" });
  const providerModels = useMemo(
    () => (typeof pickedProvider === "string" ? getProviderModels(pickedProvider, modelCostMapData) : []),
    [pickedProvider, modelCostMapData],
  );
  const decisionCatalog = useMemo(() => buildDecisionCatalog(modelCostMapData), [modelCostMapData]);
  const [showAdvancedSettings, setShowAdvancedSettings] = useState(false);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["models", "list"] });

  const mountedValues = () => projectMountedValues(registry, form.getValues);

  const handleOk = async (): Promise<boolean> => {
    const isValid = await form.trigger(registry.mountedNames() as string[]);
    if (!isValid) {
      return false;
    }
    await handleAddModelSubmit(
      mountedValues(),
      accessToken,
      { resetFields: () => form.reset(INITIAL_VALUES) },
      refresh,
    );
    return true;
  };

  return (
    <AddModelForm
      form={form}
      registry={registry}
      mountedValues={mountedValues}
      handleOk={handleOk}
      selectedProvider={selectedProvider}
      setSelectedProvider={setSelectedProvider}
      providerModels={providerModels}
      getPlaceholder={getPlaceholder}
      showAdvancedSettings={showAdvancedSettings}
      setShowAdvancedSettings={setShowAdvancedSettings}
      teams={teams ?? null}
      credentials={credentialsResponse?.credentials || []}
      decisionCatalog={decisionCatalog}
    />
  );
}
