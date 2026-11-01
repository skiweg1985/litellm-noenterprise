import { renderWithProviders as render, screen } from "../../tests/test-utils";
import userEvent, { PointerEventsCheckLevel } from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CredentialItem } from "@/components/networking";

import ModelInfoEditForm from "./ModelInfoEditForm";

vi.mock("@/lib/http/chatgptSubscriptions", () => ({
  getSubscriptions: vi.fn().mockResolvedValue({
    accounts: [
      {
        account: "fixture",
        subscription_id: "subscription-one",
        display_name: "Development",
        token_valid: true,
        retired: false,
        deployment_ids: ["model-one"],
        available_models: { models: [{ slug: "example-model", name: "Example" }], missing: [] },
      },
    ],
    problems: [],
  }),
}));

vi.mock("@/components/networking", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/networking")>();
  return {
    ...actual,
    vectorStoreListCall: vi.fn().mockResolvedValue({ data: [] }),
  };
});

const credentialsList: CredentialItem[] = [
  {
    credential_name: "openai-main",
    display_name: "Main OpenAI",
    credential_values: {},
    credential_info: { custom_llm_provider: "openai" },
  },
];

const renderForm = ({
  onSubmit = vi.fn().mockResolvedValue(undefined),
  isEditing = true,
  modelInfo = {} as Record<string, unknown>,
  litellmParams = { model: "gpt-4o" } as Record<string, unknown>,
} = {}) =>
  render(
    <ModelInfoEditForm
      localModelData={{
        model_name: "gpt-4o",
        litellm_params: litellmParams,
        model_info: modelInfo,
      }}
      modelData={{ model_info: {} }}
      teamAlias={null}
      accessToken="test-token"
      isEditing={isEditing}
      isSaving={false}
      isWildcardModel={false}
      ptuCostAttributionEnabled={false}
      showCacheControl={false}
      setShowCacheControl={vi.fn()}
      onCancel={vi.fn()}
      onSubmit={onSubmit}
      modelAccessGroups={[]}
      guardrailsList={[]}
      tagsList={{}}
      credentialsList={credentialsList}
      healthCheckModelOptions={[]}
      teams={[]}
    />,
  );

describe("ModelInfoEditForm existing-credentials picker", () => {
  it("shows the display name beside the credential name, searches by it, and submits the name", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderForm({ onSubmit });

    const picker = await screen.findByPlaceholderText("Select or search for existing credentials");
    await user.click(picker);

    const option = await screen.findByRole("option", { name: /openai-main/ });
    expect(option).toHaveTextContent("openai-main");
    expect(option).toHaveTextContent("Main OpenAI");

    await user.type(picker, "main openai");
    await user.click(await screen.findByRole("option", { name: /openai-main/ }));
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].litellm_credential_name).toBe("openai-main");
  });

  it("keeps the attached credential when the search text is emptied and only clears through None", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderForm({ onSubmit, litellmParams: { model: "gpt-4o", litellm_credential_name: "openai-main" } });

    const picker = await screen.findByPlaceholderText("Select or search for existing credentials");
    expect(picker).toHaveValue("Main OpenAI");
    await user.clear(picker);
    await user.tab();
    await user.click(screen.getByRole("button", { name: /save changes/i }));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].litellm_credential_name).toBe("openai-main");

    await user.click(picker);
    await user.click(await screen.findByRole("option", { name: "None" }));
    await user.click(screen.getByRole("button", { name: /save changes/i }));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(onSubmit.mock.calls[1][0].litellm_credential_name).toBeNull();
  });

  it("shows the attached credential's display name with its credential name beneath when not editing", () => {
    renderForm({ isEditing: false, litellmParams: { model: "gpt-4o", litellm_credential_name: "openai-main" } });

    expect(screen.getByText("Main OpenAI")).toBeInTheDocument();
    expect(screen.getByText("openai-main")).toBeInTheDocument();
  });

  it("shows the selected credential's name under the picker only when its label differs", async () => {
    const user = userEvent.setup({ pointerEventsCheck: PointerEventsCheckLevel.Never });
    renderForm({ litellmParams: { model: "gpt-4o", litellm_credential_name: "openai-main" } });

    const picker = await screen.findByPlaceholderText("Select or search for existing credentials");
    expect(picker).toHaveValue("Main OpenAI");
    expect(screen.getByText("openai-main")).toBeInTheDocument();

    await user.click(picker);
    await user.click(await screen.findByRole("option", { name: "None" }));
    expect(screen.queryByText("openai-main")).not.toBeInTheDocument();
  });

  it("falls back to the credential name, then Manual, when not editing", () => {
    const { unmount } = renderForm({
      isEditing: false,
      litellmParams: { model: "gpt-4o", litellm_credential_name: "not-listed" },
    });
    expect(screen.getAllByText("not-listed")).toHaveLength(1);
    unmount();

    renderForm({ isEditing: false });
    expect(screen.getByText("Manual")).toBeInTheDocument();
  });
});

it("shows a legacy subscription by deployment identity and submits its stable ID", async () => {
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  renderForm({
    onSubmit,
    modelInfo: { id: "model-one", db_model: true },
    litellmParams: { model: "chatgpt/example-model" },
  });
  const picker = await screen.findByRole("combobox", { name: "ChatGPT subscription" });
  await screen.findByText("Fixed subscription: requests use only this account.");
  expect(picker).toHaveValue("Development");
  expect(screen.queryByText("Existing Credentials")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Save Changes" }));
  expect(onSubmit).toHaveBeenCalledWith(
    expect.objectContaining({ subscription_id: "subscription-one" }),
    expect.any(Function),
  );
});

it("loads and edits reserve order without collapsing the saved assignment to fixed mode", async () => {
  const { getSubscriptions } = await import("@/lib/http/chatgptSubscriptions");
  const { testQueryClient } = await import("../../tests/test-utils");
  testQueryClient.clear();
  vi.mocked(getSubscriptions).mockResolvedValue({ accounts: [
    { account: "first", subscription_id: "first", display_name: "Primary", token_valid: true, retired: false, available_models: { models: [{ slug: "example-model", name: "Example" }], missing: [] } },
    { account: "second", subscription_id: "second", display_name: "Reserve", token_valid: true, retired: false, available_models: { models: [{ slug: "example-model", name: "Example" }], missing: [] } },
  ], problems: [] } as never);
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  renderForm({ onSubmit,
    modelInfo: { id: "parent-model", db_model: true, subscription_assignment: { mode: "priority", subscription_ids: ["first", "second"], revision: "saved-revision" } },
    litellmParams: { model: "chatgpt/example-model", timeout: 20 },
  });
  expect(await screen.findByRole("combobox", { name: "Subscription routing mode" })).toHaveTextContent("Preferred subscription with reserves");
  await userEvent.click(await screen.findByRole("button", { name: "Move Reserve up" }));
  await userEvent.click(screen.getByRole("button", { name: "Save Changes" }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ subscription_selection: { mode: "priority", subscription_ids: ["second", "first"] }, timeout: 20 }), expect.any(Function));
});
