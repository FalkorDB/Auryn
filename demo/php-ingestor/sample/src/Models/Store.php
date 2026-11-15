<?php

namespace App\Models;

class Store extends AbstractStore implements Repository
{
    use Timestamps;

    public const MAX_ITEMS = 100;

    /** @var string[] */
    private array $items = [];

    public function __construct(private Logger $logger)
    {
    }

    public function put(string $value): void
    {
        $this->items[] = $value;
        $this->logger->log($value);
        $this->touch();
    }

    public function size(): int
    {
        return count($this->items);
    }
}
