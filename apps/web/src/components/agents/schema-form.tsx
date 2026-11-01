"use client";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { JsonSchemaProperty, ParamsSchema } from "@/lib/agent-types";

/** JSON Schema → 동적 폼 렌더러 (string/enum/integer/boolean) */
export function SchemaForm({
  schema,
  values,
  onChange,
}: {
  schema: ParamsSchema;
  values: Record<string, unknown>;
  onChange: (values: Record<string, unknown>) => void;
}) {
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);

  function setValue(key: string, value: unknown) {
    onChange({ ...values, [key]: value });
  }

  function renderField(key: string, prop: JsonSchemaProperty) {
    const label = prop.title ?? key;
    const isRequired = required.has(key);
    const value = values[key] ?? prop.default ?? "";

    if (prop.enum) {
      return (
        <Select value={String(value)} onValueChange={(v) => setValue(key, v)}>
          <SelectTrigger id={`field-${key}`}>
            <SelectValue placeholder={label} />
          </SelectTrigger>
          <SelectContent>
            {prop.enum.map((opt) => (
              <SelectItem key={String(opt)} value={String(opt)}>
                {opt}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    if (prop.type === "boolean") {
      return (
        <Checkbox
          id={`field-${key}`}
          checked={Boolean(value)}
          onCheckedChange={(c) => setValue(key, c === true)}
        />
      );
    }
    if (prop.type === "integer" || prop.type === "number") {
      return (
        <Input
          id={`field-${key}`}
          type="number"
          min={prop.minimum ?? undefined}
          max={prop.maximum ?? undefined}
          value={String(value)}
          required={isRequired}
          onChange={(e) =>
            setValue(
              key,
              prop.type === "integer"
                ? parseInt(e.target.value || "0", 10)
                : parseFloat(e.target.value || "0"),
            )
          }
        />
      );
    }
    // string — 제목성 필드는 textarea로 (topic/지시사항)
    if (key === "topic" || (prop.title ?? "").includes("지시")) {
      return (
        <Textarea
          id={`field-${key}`}
          rows={3}
          value={String(value)}
          required={isRequired}
          placeholder={prop.description ?? undefined}
          onChange={(e) => setValue(key, e.target.value)}
        />
      );
    }
    return (
      <Input
        id={`field-${key}`}
        value={String(value)}
        required={isRequired}
        placeholder={prop.description ?? undefined}
        onChange={(e) => setValue(key, e.target.value)}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {Object.entries(properties).map(([key, prop]) => (
        <div key={key} className="flex flex-col gap-2">
          <Label htmlFor={`field-${key}`}>
            {prop.title ?? key}
            {required.has(key) && <span className="text-destructive"> *</span>}
          </Label>
          {renderField(key, prop)}
        </div>
      ))}
    </div>
  );
}

/** schema 기본값으로 초기 values 구성.
 *
 * `default: null`은 pydantic이 미지정 필드를 직렬화하며 채운 것으로,
 * "기본값 없음"과 동일하게 취급한다 — 그대로 채우면 optional string 필드에
 * `null`이 들어가 실행 시 jsonschema type 검증(`None is not of type 'string'`)에
 * 걸린다. */
export function defaultValues(schema: ParamsSchema): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, prop] of Object.entries(schema.properties ?? {})) {
    if (prop.default !== undefined && prop.default !== null) out[key] = prop.default;
  }
  return out;
}
