// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Clean oracle consumer: latestRoundData with updatedAt staleness check.
 * Should pass all checks (looks_ok).
 */
interface AggregatorV3Interface {
    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        );
}

contract CleanOracleFresh {
    AggregatorV3Interface public priceFeed;
    uint256 public constant HEARTBEAT = 1 days;

    constructor(address _priceFeed) {
        require(_priceFeed != address(0), "Zero address");
        priceFeed = AggregatorV3Interface(_priceFeed);
    }

    function getPrice() external view returns (int256) {
        (
            uint80 roundId,
            int256 price,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        ) = priceFeed.latestRoundData();

        require(startedAt > 0, "Round not started");
        require(updatedAt > 0, "Round not complete");
        require(block.timestamp - updatedAt <= HEARTBEAT, "Stale price");
        require(answeredInRound >= roundId, "Stale round");

        return price;
    }
}
