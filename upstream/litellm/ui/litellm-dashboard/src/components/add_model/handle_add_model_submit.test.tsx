import { describe, expect, it, vi } from "vitest";
import { prepareModelAddRequest } from "./handle_add_model_submit";

vi.mock("../networking", () => ({
  modelCreateCall: vi.fn(),
}));

describe("prepareModelAddRequest", () => {
  it("returns deployment data for the most basic form", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      base_model: "gpt-4",
      team_id: "team-123",
      model_access_group: ["group-1"],
      input_cost_per_token: "2000000",
      output_cost_per_token: "1000000",
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelName).toBe("Public Model");
    expect(deployment.litellmParamsObj.model).toBe("custom-model-name");
    expect(deployment.litellmParamsObj.input_cost_per_token).toBe(2);
    expect(deployment.litellmParamsObj.output_cost_per_token).toBe(1);
    expect(deployment.modelInfoObj.base_model).toBe("gpt-4");
    expect(deployment.modelInfoObj.access_groups).toEqual(["group-1"]);
    expect(deployment.modelInfoObj.team_id).toBe("team-123");
  });

  it("uses a lowercase fallback for unrecognized custom providers", async () => {
    const fallbackValues = {
      model_mappings: [
        {
          public_name: "Petals Model",
          litellm_model: "petals/model",
        },
      ],
      model_name: "petals/model",
      custom_llm_provider: "Petals",
    };

    const deployments = await prepareModelAddRequest({ ...fallbackValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.custom_llm_provider).toBe("petals");
  });

  it("ignores litellm_credential_name inside LiteLLM Params JSON", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      litellm_credential_name: "selected-credential",
      litellm_extra_params: JSON.stringify({
        litellm_credential_name: "from-json",
        timeout: 5,
      }),
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.litellm_credential_name).toBe("selected-credential");
    expect(deployment.litellmParamsObj.timeout).toBe(5);
  });

  it("keeps litellm_credential_name from LiteLLM Params JSON when no credential is selected", async () => {
    const formValues = {
      model_mappings: [
        {
          public_name: "Public Model",
          litellm_model: "litellm/public",
        },
      ],
      model_name: "custom-model-name",
      litellm_extra_params: JSON.stringify({
        litellm_credential_name: "from-json",
        timeout: 5,
      }),
      litellm_credential_name: null,
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.litellmParamsObj.litellm_credential_name).toBe("from-json");
    expect(deployment.litellmParamsObj.timeout).toBe(5);
  });

  it("saves the selected mode under model_info", async () => {
    const formValues = {
      model_mappings: [{ public_name: "Jev", litellm_model: "typesafe/jev-latest" }],
      mode: "evaluation",
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelInfoObj.mode).toBe("evaluation");
    expect(deployment.litellmParamsObj).not.toHaveProperty("mode");
  });

  it.each([
    ["OpenAI", "openai/*"],
    ["Azure_AI_Studio", "azure_ai/*"],
    ["Petals", "petals/*"],
  ])("composes wildcard names for the all-model selection", async (custom_llm_provider, wildcardModel) => {
    const formValues = {
      model_mappings: [],
      model: "all-wildcard",
      custom_llm_provider,
    };

    const deployments = await prepareModelAddRequest({ ...formValues }, "token", null);

    expect(deployments).toHaveLength(1);
    const [deployment] = deployments!;
    expect(deployment.modelName).toBe(wildcardModel);
    expect(deployment.litellmParamsObj.model).toBe(wildcardModel);
  });
});

describe("subscription model submission", () => {
  it("sends the selected subscription as model metadata without stale provider credentials", async () => {
    const values = {
      model_mappings: [{ public_name: "my-subscription-model", litellm_model: "chatgpt/example-model" }],
      custom_llm_provider: "CHATGPT",
      subscription_id: "subscription-one",
      litellm_extra_params: JSON.stringify({
        api_key: "stale-key",
        litellm_credential_name: "stale-credential",
        timeout: 20,
      }),
      model_info_params: JSON.stringify({
        owner: "example",
        subscription_assignment: { mode: "fixed", subscription_ids: ["wrong"] },
      }),
    };
    const result = await prepareModelAddRequest(values, "admin-token", null);
    expect(result?.[0].modelInfoObj).toMatchObject({
      owner: "example",
      subscription_assignment: { mode: "fixed", subscription_ids: ["subscription-one"] },
    });
    expect(result?.[0].litellmParamsObj).toMatchObject({ model: "chatgpt/example-model", timeout: 20 });
    expect(result?.[0].litellmParamsObj).not.toHaveProperty("api_key");
    expect(result?.[0].litellmParamsObj).not.toHaveProperty("litellm_credential_name");
    expect(result?.[0].litellmParamsObj).not.toHaveProperty("subscription_id");
  });
});

it("submits ordered reserves as one assignment without leaking UI fields into provider parameters", async () => {
  const deployments = await prepareModelAddRequest({
    model_mappings: [{ public_name: "Team model", litellm_model: "chatgpt/example" }],
    subscription_id: "old-legacy",
    subscription_selection: { mode: "priority", subscription_ids: ["preferred", "reserve"] },
    timeout: 20,
    api_key: "stale-key",
    model_info_params: JSON.stringify({ owner: "team", subscription_assignment: { mode: "fixed", subscription_ids: ["old"], revision: "previous" } }),
  }, "fixture-token", null);
  expect(deployments).toHaveLength(1);
  expect(deployments![0].modelInfoObj).toMatchObject({ owner: "team", subscription_assignment: { mode: "priority", subscription_ids: ["preferred", "reserve"], revision: "previous" } });
  expect(deployments![0].litellmParamsObj).toMatchObject({ model: "chatgpt/example", timeout: 20 });
  expect(deployments![0].litellmParamsObj).not.toHaveProperty("api_key");
  expect(deployments![0].litellmParamsObj).not.toHaveProperty("subscription_selection");
  expect(deployments![0].litellmParamsObj).not.toHaveProperty("subscription_id");
});
